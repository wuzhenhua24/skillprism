"""API 冒烟测试，同时覆盖“Tier 2/3 未实现”的显式拒绝。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from skill_eval_service.api.app import app, get_content_source, get_db
from skill_eval_service.content import LocalDirectorySource
from skill_eval_service.models import Base


@pytest.fixture
def client(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    skills = tmp_path / "skills"
    (skills / "demo").mkdir(parents=True)
    (skills / "demo" / "SKILL.md").write_text("---\nname: demo\n---\nbody\n")

    def _db():
        session = factory()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_content_source] = lambda: LocalDirectorySource(skills)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


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
