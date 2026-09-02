"""API 冒烟测试，覆盖触发接口的受理语义与“Tier 2/3 未实现”的显式拒绝。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from skillprism.api.app import app
from skillprism.config import reset_settings
from skillprism.db import init_db, reset_engine

#: 一次最小的合法触发。skill_id 用数字，和管理系统给的资源 ID 一致。
TRIGGER = {"skill_id": "2000705", "skill_name": "skill-file-md5", "skill_version": "2.0.0"}


@pytest.fixture
def client(tmp_path, monkeypatch, db_url):
    """把全局配置指向临时库。

    不能只覆盖 get_db：应用的 lifespan 会用**全局 engine** 检查库结构，
    只覆盖依赖的话，那个检查仍然指向默认库——本地有遗留库时测试会侥幸
    通过，干净检出的 CI 上则失败。

    这里不再准备任何 skill 内容：API 进程不下载内容，也就不需要
    content source。这条隔离本身就是被测行为之一。
    """
    monkeypatch.setenv("SKILLPRISM_DATABASE_URL", db_url)
    monkeypatch.setenv("SKILLPRISM_REPORT_ROOT", str(tmp_path / "reports"))
    monkeypatch.setenv("SKILLPRISM_WORK_ROOT", str(tmp_path / "work"))
    reset_settings()
    reset_engine()
    init_db()

    with TestClient(app) as c:
        yield c
    reset_engine()
    reset_settings()


def test_submit_is_accepted_without_touching_content(client):
    """202 是受理，不是评完。提交路径上没有任何网络调用。"""
    resp = client.post("/api/evaluations", json=TRIGGER)
    assert resp.status_code == 202
    body = resp.json()
    assert body["task_id"]
    assert body["state"] == "queued"
    assert body["deduplicated"] is False
    # 提交时还没下载，给不出真实 hash，就不要给占位值。
    assert "content_hash" not in body


def test_submit_records_the_declared_identity(client):
    """登记名与版本必须落库——worker 拿不到就只能退回用 skill_id 命名目录，
    那会让 SCHEMA.name_consistency 对每个 skill 都报一条 HIGH。"""
    task_id = client.post("/api/evaluations", json=TRIGGER).json()["task_id"]

    task = client.get(f"/api/tasks/{task_id}").json()
    assert task["skill_name"] == "skill-file-md5"
    assert task["skill_version"] == "2.0.0"
    assert task["content_hash"] is None


def test_unknown_skill_is_still_accepted(client):
    """“这个 skill 不存在”要等 worker 去取才知道，不能在提交时假装知道。

    取不到多半是网络抖动或包还没落盘，长成同步 4xx 会让调用方
    以为是自己请求错了。
    """
    resp = client.post("/api/evaluations", json={**TRIGGER, "skill_id": "nope"})
    assert resp.status_code == 202


def test_skill_name_is_required(client):
    """缺了它只能退回拿 skill_id 当目录名，那是我们制造的误报。"""
    resp = client.post("/api/evaluations", json={"skill_id": "2000705"})
    assert resp.status_code == 422


@pytest.mark.parametrize("name", ["../etc", "a/b", "", "."])
def test_unusable_skill_name_is_rejected_at_submit(client, name):
    """这个字段是调用方直接给的，当场就能改，不该拖到任务失败才说。

    materialize 里还有一道同样的校验，那里才是安全边界；这里只是提前。
    """
    resp = client.post("/api/evaluations", json={**TRIGGER, "skill_name": name})
    assert resp.status_code == 422


def test_repeat_trigger_folds_into_the_queued_task(client):
    """触发接口天然会被重试，排队中的同一个 skill 要收敛成一条。"""
    first = client.post("/api/evaluations", json=TRIGGER).json()
    second = client.post("/api/evaluations", json=TRIGGER).json()

    assert second["deduplicated"] is True
    assert second["task_id"] == first["task_id"]


def test_folding_refreshes_the_declared_version(client):
    """折叠进去的那条任务还没下载内容，跑起来取到的是最新的一份。

    版本标签必须跟着更新，否则结果会挂着旧版本号、描述的却是新内容。
    """
    first = client.post("/api/evaluations", json=TRIGGER).json()
    client.post("/api/evaluations", json={**TRIGGER, "skill_version": "2.1.0"})

    task = client.get(f"/api/tasks/{first['task_id']}").json()
    assert task["skill_version"] == "2.1.0"


def test_force_bypasses_folding(client):
    """强制重跑是人为动作，不能被去重吃掉。"""
    first = client.post("/api/evaluations", json=TRIGGER).json()
    forced = client.post("/api/evaluations", json={**TRIGGER, "force": True}).json()

    assert forced["deduplicated"] is False
    assert forced["task_id"] != first["task_id"]


def test_tier2_is_explicitly_not_implemented(client):
    """未实现要明确报错，不能静默当成 tier1 处理。"""
    resp = client.post("/api/evaluations", json={**TRIGGER, "tier": "tier2"})
    assert resp.status_code == 501


def test_evaluation_missing_is_404(client):
    assert client.get("/api/skills/2000705/evaluation").status_code == 404


def test_healthz_reports_scanner_state(client):
    body = client.get("/healthz").json()
    assert body["status"] in {"ok", "degraded"}
    assert "missing_scanners" in body
