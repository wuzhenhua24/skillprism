"""运行时评测（Tier 3）的对外接口：受理、查询、报告、与 Tier 1 结论的拼接。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skillprism.api.app import REPORT_SECURITY_HEADERS, app
from skillprism.config import reset_settings
from skillprism.db import init_db, reset_engine, session_scope
from skillprism.domain import ContentSource, EvaluationStatus
from skillprism.models import EvaluationTask, RuntimeResult
from skillprism.repository import save_result
from skillprism.schemas import EvaluationDTO, EvaluatorInfo

TRIGGER = {"skill_id": "2000705", "skill_name": "ticket-formatter", "tier": "tier3"}
PUBLIC = "https://prism.internal"
CASES = [
    {
        "case_id": "basic-format",
        "title": "Produces the fixed ticket layout",
        "status": "passed",
        "pass_rate": 1.0,
        "runs": [{"iteration": 1, "status": "PASS", "reason": None}],
    },
    {
        "case_id": "wrong",
        "title": "Wrong",
        "status": "failed",
        "pass_rate": 0.0,
        "runs": [{"iteration": 1, "status": "FAIL", "reason": "output_matches.all: missing P0"}],
    },
]


@pytest.fixture
def client(tmp_path, monkeypatch, db_url):
    monkeypatch.setenv("SKILLPRISM_DATABASE_URL", db_url)
    monkeypatch.setenv("SKILLPRISM_REPORT_ROOT", str(tmp_path / "reports"))
    monkeypatch.setenv("SKILLPRISM_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("SKILLPRISM_PUBLIC_BASE_URL", PUBLIC)
    reset_settings()
    reset_engine()
    init_db()
    with TestClient(app) as c:
        yield c
    reset_engine()
    reset_settings()


def _runtime_row(tmp_path: Path, **overrides) -> RuntimeResult:
    report = tmp_path / f"report-{uuid.uuid4().hex}.html"
    report.write_text("<html>runtime</html>")
    values = dict(
        id=str(uuid.uuid4()),
        source="local",
        skill_id="2000705",
        content_hash="sha256:c1",
        runtime_fingerprint="sha256:fp-a",
        status="failed",
        passed=1,
        failed=1,
        case_count=2,
        model="deepseek-v4-flash",
        served_models=["deepseek-v4-flash-ga-260731"],
        cases=CASES,
        report_html_uri=report.as_uri(),
        evaluated_at=datetime(2026, 9, 29, tzinfo=UTC),
    )
    values.update(overrides)
    row = RuntimeResult(**values)
    with session_scope() as db:
        db.add(row)
    return row


def test_tier3_is_accepted_into_the_sandbox_queue(client):
    body = client.post("/api/evaluations", json=TRIGGER).json()
    with session_scope() as db:
        task = db.get(EvaluationTask, body["task_id"])
        assert (task.tier, task.queue) == ("tier3", "sandbox")


def test_tier1_and_tier3_triggers_do_not_fold_into_each_other(client):
    """两层是两个任务：各自排队、各自跑，互不折叠。"""
    first = client.post("/api/evaluations", json={**TRIGGER, "tier": "tier1"}).json()
    second = client.post("/api/evaluations", json=TRIGGER).json()
    assert second["deduplicated"] is False
    assert second["task_id"] != first["task_id"]


def test_tier3_bundle_is_refused_at_submit(client):
    resp = client.post(
        "/api/evaluations", json={"skill_id": "group/repo:skills", "bundle": True, "tier": "tier3"}
    )
    assert resp.status_code == 422
    assert "bundle" in resp.json()["detail"]


def test_tier2_is_still_not_implemented(client):
    resp = client.post("/api/evaluations", json={**TRIGGER, "tier": "tier2"})
    assert resp.status_code == 501
    assert "tier1" in resp.json()["detail"] and "tier3" in resp.json()["detail"]


def test_runtime_evaluation_404_when_never_run(client):
    assert client.get("/api/skills/2000705/runtime-evaluation").status_code == 404


def test_runtime_evaluation_returns_cases_and_the_run_profile(client, tmp_path):
    _runtime_row(tmp_path)
    body = client.get(
        "/api/skills/2000705/runtime-evaluation", params={"content_hash": "sha256:c1"}
    ).json()

    assert body["status"] == "failed"
    assert body["pass_rate"] == 0.5
    assert body["runtime"]["fingerprint"] == "sha256:fp-a"
    assert body["runtime"]["served_models"] == ["deepseek-v4-flash-ga-260731"]
    assert [c["case_id"] for c in body["cases"]] == ["basic-format", "wrong"]
    assert body["cases"][1]["runs"][0]["reason"] == "output_matches.all: missing P0"
    assert body["report_url"] == (
        f"{PUBLIC}/api/skills/2000705/runtime-report"
        "?source=local&content_hash=sha256%3Ac1&fingerprint=sha256%3Afp-a"
    )


def test_the_fingerprint_pins_one_of_several_results_for_the_same_content(client, tmp_path):
    """同一份内容换个模型评过就有两条。不带指纹拿到最近的，带了拿到确定的那条。"""
    _runtime_row(tmp_path, runtime_fingerprint="sha256:old", model="old-model")
    _runtime_row(
        tmp_path,
        runtime_fingerprint="sha256:new",
        model="new-model",
        evaluated_at=datetime(2026, 9, 29, tzinfo=UTC) + timedelta(hours=1),
    )
    url = "/api/skills/2000705/runtime-evaluation"
    latest = client.get(url, params={"content_hash": "sha256:c1"}).json()
    pinned = client.get(
        url, params={"content_hash": "sha256:c1", "fingerprint": "sha256:old"}
    ).json()
    assert latest["runtime"]["model"] == "new-model"
    assert pinned["runtime"]["model"] == "old-model"


def test_runtime_report_is_served_with_the_security_headers(client, tmp_path):
    _runtime_row(tmp_path)
    resp = client.get(
        "/api/skills/2000705/runtime-report",
        params={"content_hash": "sha256:c1", "fingerprint": "sha256:fp-a"},
    )
    assert resp.status_code == 200
    assert resp.text == "<html>runtime</html>"
    for name, value in REPORT_SECURITY_HEADERS.items():
        assert resp.headers[name] == value


def test_tier1_evaluation_carries_a_tier3_summary(client, tmp_path):
    """预留的 tiers.tier3 分区：详情页不用改接口就能展示一块。"""
    with session_scope() as db:
        save_result(
            db,
            EvaluationDTO(
                skill_id="2000705",
                content_hash="sha256:c1",
                status=EvaluationStatus.PASSED,
                evaluator=EvaluatorInfo(version="0.3.0"),
            ),
            source=ContentSource.LOCAL,
        )
    no_runtime = client.get("/api/skills/2000705/evaluation").json()
    assert no_runtime["tiers"]["tier3"] is None

    _runtime_row(tmp_path)
    body = client.get("/api/skills/2000705/evaluation").json()
    assert body["status"] == "passed", "顶层仍是 Tier 1 的结论"
    tier3 = body["tiers"]["tier3"]
    assert tier3["status"] == "failed"
    assert [(v["validator"], v["passed"]) for v in tier3["validators"]] == [
        ("basic-format", True),
        ("wrong", False),
    ]
    assert tier3["validators"][1]["errors"] == ["output_matches.all: missing P0"]


def test_tier3_summary_follows_the_content_hash(client, tmp_path):
    """Tier 1 是 v1 的结论，Tier 3 只评过 v2：拼上去就是张冠李戴。"""
    with session_scope() as db:
        save_result(
            db,
            EvaluationDTO(skill_id="2000705", content_hash="sha256:v1", status=EvaluationStatus.PASSED),
            source=ContentSource.LOCAL,
        )
    _runtime_row(tmp_path, content_hash="sha256:v2")
    body = client.get("/api/skills/2000705/evaluation", params={"content_hash": "sha256:v1"}).json()
    assert body["tiers"]["tier3"] is None
