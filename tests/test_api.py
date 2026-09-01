"""API 冒烟测试，同时覆盖“Tier 2/3 未实现”的显式拒绝。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from skill_eval_service.api.app import app, get_content_source
from skill_eval_service.config import reset_settings
from skill_eval_service.content import LocalDirectorySource
from skill_eval_service.db import init_db, reset_engine


@pytest.fixture
def client(tmp_path, monkeypatch, db_url):
    """把全局配置指向临时库。

    不能只覆盖 get_db：应用的 lifespan 会用**全局 engine** 检查库结构，
    只覆盖依赖的话，那个检查仍然指向默认库——本地有遗留库时测试会侥幸
    通过，干净检出的 CI 上则失败。
    """
    skills = tmp_path / "skills"
    (skills / "demo").mkdir(parents=True)
    (skills / "demo" / "SKILL.md").write_text("---\nname: demo\n---\nbody\n")

    monkeypatch.setenv("SES_DATABASE_URL", db_url)
    monkeypatch.setenv("SES_REPORT_ROOT", str(tmp_path / "reports"))
    monkeypatch.setenv("SES_WORK_ROOT", str(tmp_path / "work"))
    reset_settings()
    reset_engine()
    init_db()

    app.dependency_overrides[get_content_source] = lambda: LocalDirectorySource(skills)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_engine()
    reset_settings()


def test_submit_creates_task(client):
    resp = client.post("/api/evaluations", json={"skill_id": "demo"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"]
    assert body["content_hash"].startswith("sha256:")
    assert body["cached"] is False


def test_submit_unknown_skill_is_404(client):
    resp = client.post("/api/evaluations", json={"skill_id": "nope"})
    assert resp.status_code == 404


def test_tier2_is_explicitly_not_implemented(client):
    """未实现要明确报错，不能静默当成 tier1 处理。"""
    resp = client.post("/api/evaluations", json={"skill_id": "demo", "tier": "tier2"})
    assert resp.status_code == 501


def test_evaluation_missing_is_404(client):
    assert client.get("/api/skills/demo/evaluation").status_code == 404


def test_healthz_reports_scanner_state(client):
    body = client.get("/healthz").json()
    assert body["status"] in {"ok", "degraded"}
    assert "missing_scanners" in body
