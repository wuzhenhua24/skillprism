"""两个接入各走一个接口，查询侧用 ``?source=`` 指明命名空间。

拆开的理由不是字段名好看：zip 与 GitLab 对"哪个 skill"和"哪个版本"的解释
不同，塞进同一组字段的话，服务端只能按进程配置猜是哪一种——猜错不报错，
只会默默评错东西。入口把这件事变成调用方的声明。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from skillprism.api.app import app
from skillprism.config import reset_settings
from skillprism.db import init_db, reset_engine, session_scope
from skillprism.models import EvaluationResult, EvaluationTask

GITLAB = "https://gitlab.internal"
TEMPLATE = "https://mgmt.internal/api/resource/{skill_id}/download"


def _client(tmp_path, monkeypatch, db_url, **env) -> TestClient:
    monkeypatch.setenv("SKILLPRISM_DATABASE_URL", db_url)
    monkeypatch.setenv("SKILLPRISM_REPORT_ROOT", str(tmp_path / "reports"))
    monkeypatch.setenv("SKILLPRISM_WORK_ROOT", str(tmp_path / "work"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    reset_settings()
    reset_engine()
    init_db()
    return TestClient(app)


@pytest.fixture
def both(tmp_path, monkeypatch, db_url):
    """两种接入同时启用——这一步之前这个配置会让服务起不来。"""
    client = _client(
        tmp_path, monkeypatch, db_url,
        SKILLPRISM_GITLAB_BASE_URL=GITLAB,
        SKILLPRISM_CONTENT_URL_TEMPLATE=TEMPLATE,
    )
    with client as c:
        yield c
    reset_engine()
    reset_settings()


@pytest.fixture
def zip_only(tmp_path, monkeypatch, db_url):
    client = _client(tmp_path, monkeypatch, db_url, SKILLPRISM_CONTENT_URL_TEMPLATE=TEMPLATE)
    with client as c:
        yield c
    reset_engine()
    reset_settings()


def _task(task_id: str) -> EvaluationTask:
    with session_scope() as db:
        task = db.get(EvaluationTask, task_id)
        db.expunge(task)
        return task


# ---- GitLab 入口：三个字段折成内部的两列 ----


def test_gitlab_endpoint_builds_the_internal_identity(both):
    """对外 project + subdir + ref，对内仍是 skill_id + skill_version。

    落库形态不变是有意的：已有的结论、查询与报告地址都按那两列寻址。
    """
    resp = both.post(
        "/api/evaluations/gitlab",
        json={
            "project": "group/repo",
            "subdir": "skills/log-triage",
            "ref": "v1.2.0",
            "skill_name": "log-triage",
        },
    )
    assert resp.status_code == 202

    task = _task(resp.json()["task_id"])
    assert task.source == "gitlab"
    assert task.skill_id == "group/repo:skills/log-triage"
    assert task.skill_version == "v1.2.0"


def test_gitlab_endpoint_leaves_a_missing_ref_empty(both):
    """留空的 ref 不在这里补成 DEFAULT_REF。

    补了的话，"没指定"和"显式写了 main"在去重键上就是两个值，而它们指的
    是同一份内容。默认值由取内容那一层用。
    """
    resp = both.post(
        "/api/evaluations/gitlab",
        json={"project": "group/repo", "skill_name": "log-triage"},
    )
    assert _task(resp.json()["task_id"]).skill_version is None


def test_gitlab_endpoint_takes_a_bundle_without_a_skill_name(both):
    """整组触发时登记名没有位置可放——目录有 N 个，名字只有一个。

    GitLab 是 bundle 的主要来源（plugin 仓的 skills/ 那一层），所以这条要在
    具名入口上单独钉住，不能只靠保留通道的测试。
    """
    resp = both.post(
        "/api/evaluations/gitlab",
        json={"project": "group/repo", "subdir": "skills", "bundle": True},
    )
    assert resp.status_code == 202

    task = _task(resp.json()["task_id"])
    assert task.bundle is True
    assert task.skill_name is None


def test_gitlab_endpoint_still_requires_a_skill_name_for_a_single_skill(both):
    """单 skill 那边一个字没松：缺了就只能拿 skill_id 当目录名，
    而 GitLab 下它是 ``项目:子目录``，name_consistency 必报。"""
    resp = both.post(
        "/api/evaluations/gitlab",
        json={"project": "group/repo", "subdir": "skills/log-triage"},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"project": "group/repo", "subdir": "../etc"}, "子目录逃出仓库"),
        ({"project": "group/repo", "subdir": "a:b"}, "子目录里带分隔符"),
        ({"project": "group/repo", "ref": "feat branch"}, "ref 含空格"),
        ({"project": "group/repo", "ref": "a..b"}, "ref 含非法序列"),
        ({"project": "../repo"}, "项目路径逃出去"),
    ],
)
def test_gitlab_endpoint_rejects_bad_identity_on_submit(both, payload, reason):
    """写错的位置或 ref 当场 422。

    这正是拆开入口换来的：塞在一个 skill_id 字符串里时，这些错要等 worker
    去取内容才暴露，表现成一条十秒后失败的任务。
    """
    resp = both.post(
        "/api/evaluations/gitlab", json={**payload, "skill_name": "log-triage"}
    )
    assert resp.status_code == 422, reason


def test_gitlab_endpoint_is_closed_when_the_source_is_off(zip_only):
    """没启用的接入不收任务——收了也没人跑，会一直排在队列里。"""
    resp = zip_only.post(
        "/api/evaluations/gitlab",
        json={"project": "group/repo", "skill_name": "log-triage"},
    )
    assert resp.status_code == 409
    assert "gitlab" in resp.json()["detail"]


# ---- zip 入口 ----


def test_zip_endpoint_records_its_own_source(both):
    resp = both.post(
        "/api/evaluations/zip",
        json={"skill_id": "2000705", "skill_name": "skill-file-md5", "skill_version": "2.0.0"},
    )
    assert resp.status_code == 202
    assert _task(resp.json()["task_id"]).source == "zip"


def test_the_same_id_under_both_sources_is_two_tasks(both):
    """管理系统的资源 ID 42 和 GitLab 的数字项目 ID 42 是两个东西。"""
    first = both.post(
        "/api/evaluations/zip", json={"skill_id": "42", "skill_name": "demo"}
    ).json()
    second = both.post(
        "/api/evaluations/gitlab", json={"project": "42", "skill_name": "demo"}
    ).json()

    assert second["deduplicated"] is False
    assert first["task_id"] != second["task_id"]
    assert _task(first["task_id"]).source == "zip"
    assert _task(second["task_id"]).source == "gitlab"


# ---- 保留通道 ----


def test_the_legacy_endpoint_still_works_with_one_source(zip_only):
    """只启用一种时行为和拆分之前完全一致，管理系统不必跟着改。"""
    resp = zip_only.post(
        "/api/evaluations", json={"skill_id": "2000705", "skill_name": "demo"}
    )
    assert resp.status_code == 202
    assert _task(resp.json()["task_id"]).source == "zip"


def test_the_legacy_endpoint_refuses_to_guess_when_both_are_on(both):
    """两种都启用时这个请求说不清是哪一种，挑一个当默认会默默评错东西。"""
    resp = both.post("/api/evaluations", json={"skill_id": "42", "skill_name": "demo"})

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "source" in detail and "zip" in detail and "gitlab" in detail


# ---- 查询侧的 ?source= ----

#: 两个来源下恰好撞在一起的 ID。
COLLIDING_ID = "42"


def _seed_both_sources(report_root) -> None:
    """同一个 ID 在两个来源下各一条结论，分数不同好分辨。"""
    import uuid

    with session_scope() as session:
        for source, score in (("zip", 90.0), ("gitlab", 10.0)):
            report = report_root / source / "report.html"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(f"<h1>{source}</h1>", encoding="utf-8")
            session.add(
                EvaluationResult(
                    id=str(uuid.uuid4()),
                    source=source,
                    skill_id=COLLIDING_ID,
                    content_hash=f"sha256:{source}",
                    status="passed",
                    score=score,
                    severity_counts={},
                    incomplete_scans=[],
                    report_html_uri=f"file://{report}",
                )
            )


def test_query_picks_the_namespace_by_source(both, tmp_path):
    """带上 source 才知道去哪个命名空间找。两条结论互不遮挡。"""
    _seed_both_sources(tmp_path / "reports")

    from_zip = both.get(f"/api/skills/{COLLIDING_ID}/evaluation?source=zip").json()
    from_gitlab = both.get(f"/api/skills/{COLLIDING_ID}/evaluation?source=gitlab").json()

    assert (from_zip["score"], from_gitlab["score"]) == (90.0, 10.0)


def test_query_without_source_refuses_to_guess_when_both_are_on(both, tmp_path):
    """省了 source 就可能取到另一个接入下同名的那条，而且看起来完全正常。"""
    _seed_both_sources(tmp_path / "reports")

    resp = both.get(f"/api/skills/{COLLIDING_ID}/evaluation")
    assert resp.status_code == 400
    assert "source" in resp.json()["detail"]


def test_query_without_source_is_fine_with_one_source(zip_only, tmp_path):
    """只启用一种时不用带——管理系统那边的老调用不必跟着改。"""
    _seed_both_sources(tmp_path / "reports")

    dto = zip_only.get(f"/api/skills/{COLLIDING_ID}/evaluation").json()
    assert dto["score"] == 90.0


def test_a_disabled_source_can_still_be_queried(zip_only, tmp_path):
    """接入停掉了，停掉之前评出来的结论还在库里，仍然该查得到。

    所以查询侧只校验 source 取值合法，不要求它当前启用。
    """
    _seed_both_sources(tmp_path / "reports")

    dto = zip_only.get(f"/api/skills/{COLLIDING_ID}/evaluation?source=gitlab").json()
    assert dto["score"] == 10.0


def test_an_unknown_source_is_rejected(both):
    resp = both.get(f"/api/skills/{COLLIDING_ID}/evaluation?source=svn")
    assert resp.status_code == 422
    assert "svn" in resp.json()["detail"]


def test_the_report_endpoint_follows_the_same_source(both, tmp_path):
    """结论和报告必须走同一条查找逻辑，否则会出现"结论查准了、
    点开报告却是另一个接入下同名 skill 的那份"。"""
    _seed_both_sources(tmp_path / "reports")

    assert "gitlab" in both.get(f"/api/skills/{COLLIDING_ID}/report?source=gitlab").text
    assert "zip" in both.get(f"/api/skills/{COLLIDING_ID}/report?source=zip").text
