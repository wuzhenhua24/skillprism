"""HTTP API。管理系统通过这里提交评测与读取结果。"""

from __future__ import annotations

from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from skillprism import service
from skillprism.config import get_settings
from skillprism.content import SkillNotFoundError, enabled_sources
from skillprism.db import SCHEMA_NOT_READY_HINT, get_session_factory, schema_is_ready
from skillprism.domain import ContentSource
from skillprism.embedding_shim import router as embedding_shim_router
from skillprism.materialize import MaterializeError, UnsafePathError
from skillprism.models import EvaluationTask
from skillprism.runner import preflight
from skillprism.schemas import (
    EvaluationDTO,
    GitLabSubmitRequest,
    SubmitRequest,
    SubmitResponse,
)

@asynccontextmanager
async def lifespan(_: FastAPI):
    get_settings().ensure_dirs()
    if not schema_is_ready():
        raise RuntimeError(SCHEMA_NOT_READY_HINT)
    yield


app = FastAPI(
    title="SkillPrism",
    description="基于 SkillEvaluator 的 Tier 1 评测编排与结果服务",
    version="0.1.0",
    lifespan=lifespan,
)


# Embedding 批量拆分 shim。Tier 2 的 worker 把 SKILL_EVAL_EMBEDDING_BASE_URL
# 指向 <本服务>/embed/v1，而不是直连方舟。原因见 embedding_shim 模块文档。
app.include_router(embedding_shim_router, prefix="/embed/v1", tags=["embedding-shim"])


def enabled() -> tuple[ContentSource, ...]:
    return enabled_sources(get_settings())


def require_enabled(kind: ContentSource) -> ContentSource:
    """这个接入在本部署上得是开着的。

    没开还照收的话，任务会一直排在队列里没人处理——worker 手上没有对应的
    客户端。宁可在提交时就说清楚。
    """
    if kind not in enabled():
        raise HTTPException(
            status_code=409,
            detail=(
                f"本部署未启用 {kind} 接入（当前启用："
                f"{'、'.join(str(k) for k in enabled())}）"
            ),
        )
    return kind


def default_source() -> ContentSource:
    """没指定来源时用哪个。只启用了一种就是它，多种时说不清，要求指明。

    不挑一个当默认：两种接入对 skill_id 的解释不同，挑错了不会报错，
    只会取到另一个命名空间里同名的东西。
    """
    kinds = enabled()
    if len(kinds) == 1:
        return kinds[0]
    raise HTTPException(
        status_code=400,
        detail=(
            f"本部署同时启用了 {'、'.join(str(k) for k in kinds)} 接入，"
            "请用 source= 指明这次要哪一个"
        ),
    )


def query_source(source: str | None) -> ContentSource:
    """查询侧的来源。

    只校验取值合法，**不要求它当前启用**：接入可以停掉，停掉之前评出来的
    结论还在库里，那些结论仍然该查得到。
    """
    if source is None:
        return default_source()
    try:
        return ContentSource(source)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"source 只能是 {'、'.join(str(k) for k in ContentSource)}，收到 {source!r}",
        ) from None


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


def _accept(
    session: Session, request: SubmitRequest, source: ContentSource
) -> SubmitResponse:
    """两个入口共用的受理逻辑。

    这里不下载内容，因此也不会返回"skill 不存在"——那要等 worker 真的去取
    才知道，届时体现为任务的 error 状态。取不到多半是网络抖动或包还没落盘，
    把它长成一个同步的 4xx 会让调用方以为是自己请求错了。
    """
    if request.tier not in service.IMPLEMENTED_TIERS:
        raise HTTPException(status_code=501, detail=f"{request.tier} 尚未实现，当前仅支持 tier1")
    try:
        return service.submit(session, request, source=source)
    except (UnsafePathError, MaterializeError) as exc:
        # 只可能来自 skill_name 校验：这个字段是调用方直接给的，当场就能改。
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/evaluations/zip", response_model=SubmitResponse, status_code=202)
def submit_zip_evaluation(
    request: SubmitRequest,
    session: Session = Depends(get_db),
) -> SubmitResponse:
    """按管理系统的 zip 下载接口触发。202 表示受理，不表示评完。

    ``skill_id`` 是管理系统的资源 ID，``skill_version`` 是用户上传时手填的
    自由文本标签——它不决定取到哪份内容，所以排队中换个版本号会折叠进同一条
    任务并刷新标签。
    """
    return _accept(session, request, require_enabled(ContentSource.ZIP))


@app.post("/api/evaluations/gitlab", response_model=SubmitResponse, status_code=202)
def submit_gitlab_evaluation(
    request: GitLabSubmitRequest,
    session: Session = Depends(get_db),
) -> SubmitResponse:
    """按 GitLab 仓库触发。202 表示受理，不表示评完。

    位置是 ``project`` + ``subdir``，版本是 ``ref``。ref 决定取到哪个 commit，
    因此两个 ref 是两次评测，不会互相折叠。

    三个字段的合法性当场校验：写错的项目路径、子目录或 ref 重试多少次都
    一样，422 比十秒后一条失败任务好查。
    """
    source = require_enabled(ContentSource.GITLAB)
    try:
        internal = service.to_submit_request(request)
    except SkillNotFoundError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _accept(session, internal, source)


@app.post("/api/evaluations", response_model=SubmitResponse, status_code=202)
def submit_evaluation(
    request: SubmitRequest,
    session: Session = Depends(get_db),
) -> SubmitResponse:
    """保留通道：不指明接入的触发。

    只启用了一种接入时按那一种解释请求体（GitLab 接入下 ``skill_id`` 仍是
    ``项目[:子目录]``、``skill_version`` 仍是 ref，与拆分之前一致）。两种都
    启用时说不清是哪一种，返回 400 要求改用 ``/zip`` 或 ``/gitlab``——挑一个
    当默认不会报错，只会默默评错东西。
    """
    return _accept(session, request, default_source())


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str, session: Session = Depends(get_db)) -> dict:
    task = session.get(EvaluationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "task_id": task.id,
        # 任务自己记着来源，不是现读配置：排队期间配置可能已经改了，
        # 而这条任务属于它入队时的那个来源。
        "source": task.source,
        "skill_id": task.skill_id,
        "skill_name": task.skill_name,
        "skill_version": task.skill_version,
        # 入队时为空，worker 下载完内容才填上。
        "content_hash": task.content_hash,
        "tier": task.tier,
        "queue": task.queue,
        "state": task.state,
        "attempts": task.attempts,
        "error": task.error,
        # 非空即"正在退避、还没到重试时间"。queued 的任务光看 state 分不出
        # 是在排队还是在重试，这个字段和 error 一起才说得清。
        "next_attempt_at": task.next_attempt_at,
    }


#: 报告响应的安全头。报告是 SkillEvaluator 自生成的 HTML，内容源头是用户
#: 上传的 skill，而它现在挂在我们自己的域名下、由最终用户直接点开。
#:
#: 实测（skillevaluator 0.2.1）：正文渲染做了 HTML 转义，但报告末尾把整份
#: JSON 原样嵌在 ``<script id="report-data" type="application/json">`` 里，
#: 那个位置**没有**转义——只要有字面量 ``</script`` 进去，块就被提前闭合。
#: 试过的两条回显通道（死链目标、代码块正文）都没能把它送进去，所以没有
#: 证明可利用；但挡住它的是上游各通道顺手做的归一化，不是那个位置有转义。
#:
#: 这几个头能做的和不能做的要说清楚：``default-src 'none'`` 挡住资源加载与
#: 请求发起（fetch / img / form），``script-src 'unsafe-inline'`` 是报告自己
#: 的内联脚本要用的，所以**挡不住注入的脚本执行**，也挡不住它靠跳转把数据
#: 带走。真正的隔离是把报告放在独立域名下——那样它拿不到管理系统的 cookie
#: 与 localStorage。
#:
#: 报告是完全自包含的（无外链资源、无 fetch/XHR，一个内联 style 加一个内联
#: script），所以这套 CSP 不影响它正常渲染。
REPORT_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src data:; base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    # 报告地址里带着 skill_id、来源与 content_hash，别随跳转漏出去。
    "Referrer-Policy": "no-referrer",
}


def _no_result_detail(content_hash: str | None) -> str:
    """带了 content_hash 却查不到，与"这个 skill 从没评过"是两回事。

    前者多半是调用方把 hash 记串了或内容还没评完，后者才该去看有没有触发。
    文案分开，免得对接时两种情况都往"没触发"上查。
    """
    if content_hash:
        return f"该 skill 没有 content_hash={content_hash} 的评测结果"
    return "该 skill 尚无评测结果"


@app.get("/api/skills/{skill_id:path}/evaluation", response_model=EvaluationDTO)
def get_evaluation(
    skill_id: str,
    source: str | None = None,
    content_hash: str | None = None,
    session: Session = Depends(get_db),
) -> EvaluationDTO:
    """取一条结论。

    ``source`` 指明在哪个接入的命名空间里找。只启用了一种接入时可以省略；
    两种都启用时必须带——``skill_id`` 只在一个来源内部唯一，省了就可能取到
    另一个接入下同名的那条。
    """
    dto = service.get_evaluation(
        session,
        query_source(source),
        skill_id,
        content_hash=content_hash,
        public_base_url=get_settings().public_base_url,
    )
    if dto is None:
        raise HTTPException(status_code=404, detail=_no_result_detail(content_hash))
    return dto


@app.get("/api/skills/{skill_id:path}/report")
def get_report(
    skill_id: str,
    source: str | None = None,
    content_hash: str | None = None,
    session: Session = Depends(get_db),
) -> FileResponse:
    """回传 SkillEvaluator 生成的 HTML 报告。

    这是自生成 HTML，管理系统应以沙箱化 iframe 或独立页面承载，
    不要内联进自身 DOM。配了 ``SKILLPRISM_PUBLIC_BASE_URL`` 之后，这个地址
    会作为 ``report_url`` 随结论一起回给管理系统，由最终用户直接点开——
    所以响应带上 :data:`REPORT_SECURITY_HEADERS`。

    ``source`` 与 ``content_hash`` 都和 ``/evaluation`` 同义，两边必须一起带：
    只在一边带，拿到的结论和报告可能来自不同版本、甚至不同接入。
    """
    row = service.lookup_result(
        session, query_source(source), skill_id, content_hash=content_hash
    )
    path = service.report_path(row.report_html_uri) if row else None
    if path is None:
        detail = "报告不存在" if row else _no_result_detail(content_hash)
        raise HTTPException(status_code=404, detail=detail)
    return FileResponse(path, media_type="text/html", headers=REPORT_SECURITY_HEADERS)
