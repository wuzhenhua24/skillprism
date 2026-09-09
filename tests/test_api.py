"""API 冒烟测试，覆盖触发接口的受理语义与“Tier 2/3 未实现”的显式拒绝。"""

from __future__ import annotations

from pathlib import Path

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


def test_bundle_does_not_require_skill_name(client):
    """一组 skill 只能给一个登记名，而它要命名的目录有 N 个——所以整组触发时
    这个字段根本没有位置可放，不该拿"必填"逼调用方编一个。

    整组物化时 catalog 根固定叫 skills/，成员目录名取仓库里的那个
    （materialize_bundle），成员指纹也按目录名算（worker._process_bundle）。
    登记名从头到尾没有出场。
    """
    resp = client.post(
        "/api/evaluations", json={"skill_id": "group/repo:skills", "bundle": True}
    )
    assert resp.status_code == 202


def test_bundle_drops_the_skill_name_instead_of_echoing_it(client):
    """传了不报错——保留通道的老调用方还在传——但不能存下来再回显。

    存下来的后果就是这个字段一直以来的问题：任务状态里出现一个看起来权威、
    实际没参与任何计算的名字，对接方于是以为自己得为 47 个成员挑对一个。
    """
    task_id = client.post(
        "/api/evaluations",
        json={"skill_id": "group/repo:skills", "skill_name": "dev-workflow", "bundle": True},
    ).json()["task_id"]

    assert client.get(f"/api/tasks/{task_id}").json()["skill_name"] is None


def test_bundle_does_not_reject_a_path_shaped_skill_name(client):
    """整组触发时这个字段不当目录名用，就不该拿目录名的规则去拦它。

    这是"必填但无效"最刺眼的一面：调用方为了过校验被迫改一个随后就被丢弃
    的值，还以为自己填错了东西。
    """
    resp = client.post(
        "/api/evaluations",
        json={"skill_id": "group/repo:skills", "skill_name": "skills/dev-workflow", "bundle": True},
    )
    assert resp.status_code == 202


def test_folding_into_a_bundle_clears_the_declared_name(client):
    """折叠会翻转形态（existing.bundle），名字必须跟着形态走。

    留着上一次单 skill 触发填的名字，那条任务就会以 bundle 形态跑、却带着一个
    只对单 skill 成立的登记名——正是这个字段该被清掉的场景。
    """
    first = client.post("/api/evaluations", json=TRIGGER).json()
    folded = client.post(
        "/api/evaluations", json={"skill_id": TRIGGER["skill_id"], "bundle": True}
    ).json()

    assert folded["deduplicated"] is True
    assert folded["task_id"] == first["task_id"]
    task = client.get(f"/api/tasks/{first['task_id']}").json()
    assert task["bundle"] is True
    assert task["skill_name"] is None


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


@pytest.fixture
def gitlab_client(tmp_path, monkeypatch, db_url):
    """同 client，但配成 GitLab 接入——此时 skill_version 是 ref。"""
    monkeypatch.setenv("SKILLPRISM_DATABASE_URL", db_url)
    monkeypatch.setenv("SKILLPRISM_REPORT_ROOT", str(tmp_path / "reports"))
    monkeypatch.setenv("SKILLPRISM_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("SKILLPRISM_GITLAB_BASE_URL", "https://gitlab.internal")
    reset_settings()
    reset_engine()
    init_db()

    with TestClient(app) as c:
        yield c
    reset_engine()
    reset_settings()


def test_gitlab_mode_does_not_fold_across_refs(gitlab_client):
    """GitLab 接入下 skill_version 是 ref，两个 ref 是两份内容。

    折叠会让 v1 那次触发拿到 v2 的结论——和 find_queued 不折叠 running
    任务是同一个错误，只不过错在版本维度上。
    """
    trigger = {"skill_id": "group/repo", "skill_name": "log-triage"}
    first = gitlab_client.post(
        "/api/evaluations", json={**trigger, "skill_version": "v1.0.0"}
    ).json()
    second = gitlab_client.post(
        "/api/evaluations", json={**trigger, "skill_version": "v2.0.0"}
    ).json()

    assert second["deduplicated"] is False
    assert second["task_id"] != first["task_id"]

    # 同一个 ref 的重复触发仍然要收敛——触发接口天然会被重试。
    again = gitlab_client.post(
        "/api/evaluations", json={**trigger, "skill_version": "v1.0.0"}
    ).json()
    assert again["deduplicated"] is True
    assert again["task_id"] == first["task_id"]


def test_gitlab_mode_folds_when_no_ref_declared(gitlab_client):
    """都不给 ref 就是都走默认分支，仍然是同一份内容，该折叠。"""
    trigger = {"skill_id": "group/repo", "skill_name": "log-triage"}
    first = gitlab_client.post("/api/evaluations", json=trigger).json()
    second = gitlab_client.post("/api/evaluations", json=trigger).json()

    assert second["deduplicated"] is True
    assert second["task_id"] == first["task_id"]


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


# ---- 结论与报告的定位 ----


def _seed_two_versions(skill_id: str, report_root: Path):
    """同一个 skill_id 下两个 ref 的结论。GitLab 接入下这是常态：
    skill_id 是仓库路径、长期不变，不像 zip 接入每次上传换一个资源 ID。"""
    import datetime as dt
    import uuid

    from skillprism.db import session_scope
    from skillprism.models import EvaluationResult

    rows = [
        ("v1.0.0", "hash-v1", dt.datetime(2026, 9, 1), 90.0),
        ("main", "hash-main", dt.datetime(2026, 9, 5), 40.0),
    ]
    with session_scope() as session:
        for ref, content_hash, when, score in rows:
            report = report_root / content_hash / "report.html"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(f"<h1>{ref}</h1>", encoding="utf-8")
            session.add(
                EvaluationResult(
                    id=str(uuid.uuid4()),
                    source="local",
                    skill_id=skill_id,
                    skill_version=ref,
                    content_hash=content_hash,
                    status="passed",
                    evaluated_at=when,
                    score=score,
                    severity_counts={},
                    incomplete_scans=[],
                    report_html_uri=f"file://{report}",
                )
            )


#: 承载报告的内网域名。进程绑的是 127.0.0.1，公开地址由前置网关决定。
PUBLIC_BASE_URL = "https://skillprism.internal"


@pytest.fixture
def public_domain(client, monkeypatch):
    """配上承载报告的域名。依赖 client 是为了排在它之后——它会重置配置。"""
    monkeypatch.setenv("SKILLPRISM_PUBLIC_BASE_URL", PUBLIC_BASE_URL)
    reset_settings()
    return PUBLIC_BASE_URL


def test_report_url_pins_the_hash_of_the_row_it_came_with(client, public_domain, tmp_path):
    """链接必须钉死这条结论的 content_hash，不能是"该 skill 最近那条"。

    不带 hash 的链接会随后续评测漂走：GitLab 接入下 skill_id 是长期不变的
    仓库路径，多个 ref 的结论堆在同一个 ID 下。链接会进管理系统的库长期
    存在，那时指错版本比现在难查得多。

    上下文同样要钉住：同一个 skill 单独评过、又在一组里评过时，两条结论的
    (skill_id, content_hash) 一模一样，只带 content_hash 的链接会在两条之间
    跳。单独评的那条上下文为空，链接里就是个空值——那不是"没写"，是"要单独
    评的那条"。
    """
    skill_id = "group/repo:skills/log-triage"
    _seed_two_versions(skill_id, tmp_path / "reports")

    # 查询本身不带 hash，取到的是最近那条；链接要带的是**它**的 hash。
    dto = client.get(f"/api/skills/{skill_id}/evaluation").json()

    assert dto["content_hash"] == "hash-main"
    assert dto["report_url"] == (
        f"{PUBLIC_BASE_URL}/api/skills/group/repo:skills/log-triage"
        "/report?source=local&content_hash=hash-main&context_hash="
    )


def test_report_url_round_trips_to_that_report(client, public_domain, tmp_path):
    """拼出来的链接得真能取到报告——skill_id 里的 / 和 : 都要原样保留。"""
    from urllib.parse import urlsplit

    skill_id = "group/repo:skills/log-triage"
    _seed_two_versions(skill_id, tmp_path / "reports")

    dto = client.get(
        f"/api/skills/{skill_id}/evaluation", params={"content_hash": "hash-v1"}
    ).json()
    parts = urlsplit(dto["report_url"])
    report = client.get(f"{parts.path}?{parts.query}")

    assert report.status_code == 200
    assert "v1.0.0" in report.text


def test_report_url_is_null_without_public_base_url(client, tmp_path):
    """没配域名就是 null，不能回落到存储 URI——那是服务器本地路径。"""
    skill_id = "group/repo:skills/log-triage"
    _seed_two_versions(skill_id, tmp_path / "reports")

    dto = client.get(f"/api/skills/{skill_id}/evaluation").json()

    assert dto["report_url"] is None
    assert "file://" not in client.get(f"/api/skills/{skill_id}/evaluation").text


def test_no_report_url_when_the_row_has_no_report(client, public_domain):
    """宁可没有链接，也不给一个点开是 404 的链接——后者会被当成服务坏了。"""
    import uuid

    from skillprism.db import session_scope
    from skillprism.models import EvaluationResult

    with session_scope() as session:
        session.add(
            EvaluationResult(
                id=str(uuid.uuid4()),
                source="local",
                skill_id="2000705",
                content_hash="hash-no-report",
                status="error",
                severity_counts={},
                incomplete_scans=[],
                report_html_uri=None,
            )
        )

    dto = client.get("/api/skills/2000705/evaluation").json()

    assert dto["report_url"] is None


def test_report_response_carries_security_headers(client, tmp_path):
    """报告是自生成 HTML、内容源头是用户上传的 skill，现在由用户直接点开。

    这几个头挡不住注入的脚本执行（报告自己的内联脚本要 unsafe-inline），
    挡的是资源加载与请求发起。真正的隔离是独立域名，见 app 里那段注释。
    """
    skill_id = "group/repo:skills/log-triage"
    _seed_two_versions(skill_id, tmp_path / "reports")

    report = client.get(f"/api/skills/{skill_id}/report", params={"content_hash": "hash-v1"})

    assert report.status_code == 200
    assert report.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in report.headers["content-security-policy"]


def test_report_follows_content_hash(client, tmp_path):
    """结论查准了、点开报告却是另一份——补 content_hash 之前 /report 就是这样。

    两个端点必须走同一条查找逻辑，见 service.lookup_result。
    """
    skill_id = "group/repo:skills/log-triage"
    _seed_two_versions(skill_id, tmp_path / "reports")

    evaluation = client.get(
        f"/api/skills/{skill_id}/evaluation", params={"content_hash": "hash-v1"}
    ).json()
    report = client.get(f"/api/skills/{skill_id}/report", params={"content_hash": "hash-v1"})

    assert evaluation["skill_version"] == "v1.0.0"
    assert report.status_code == 200
    assert "v1.0.0" in report.text


def test_report_without_hash_still_returns_latest(client, tmp_path):
    """不带 hash 的老用法不变：取最近评完的那条。"""
    skill_id = "group/repo:skills/log-triage"
    _seed_two_versions(skill_id, tmp_path / "reports")

    report = client.get(f"/api/skills/{skill_id}/report")
    assert report.status_code == 200
    assert "main" in report.text


def test_unknown_hash_is_distinguished_from_never_evaluated(client, tmp_path):
    """带了 hash 却查不到，与"这个 skill 从没评过"是两回事，别都往"没触发"上查。"""
    skill_id = "group/repo:skills/log-triage"
    _seed_two_versions(skill_id, tmp_path / "reports")

    missing = client.get(
        f"/api/skills/{skill_id}/evaluation", params={"content_hash": "hash-nope"}
    )
    never = client.get("/api/skills/group/other/evaluation")

    assert missing.status_code == never.status_code == 404
    assert "hash-nope" in missing.json()["detail"]
    assert "尚无评测结果" in never.json()["detail"]


def test_gitlab_style_skill_id_routes_correctly(client, tmp_path):
    """skill_id 含 `:` 和 `/`，两种写法都要能路由到同一条结论。"""
    skill_id = "group/repo:skills/log-triage"
    _seed_two_versions(skill_id, tmp_path / "reports")

    plain = client.get(f"/api/skills/{skill_id}/evaluation")
    encoded = client.get(f"/api/skills/{skill_id.replace('/', '%2F')}/evaluation")

    assert plain.status_code == encoded.status_code == 200
    assert plain.json()["skill_version"] == encoded.json()["skill_version"] == "main"


def test_healthz_reports_scanner_state(client):
    body = client.get("/healthz").json()
    assert body["status"] in {"ok", "degraded"}
    assert "missing_scanners" in body


# ---- 任务接口交回的寻址键 ----
#
# 调用方的直觉流程是：提交 → 轮任务 → 拿任务上的 hash 去查结论。单任务时
# 这条路是通的；bundle 时任务上那个 hash 是**整组**的指纹（每条成员结论的
# context_hash），拿它查什么都是 404，而且提交、轮询、查询三步全都返回
# 正常。所以任务接口必须直接把成员各自的寻址键交出来。

BUNDLE_ID = "group/repo:skills"
CONTEXT = "sha256:whole-group"


def _seed_bundle_task(members: dict[str, str], *, source: str = "local") -> str:
    """一条评完的 bundle 任务，外加它产出的成员结论。

    ``members`` 是 成员名 → 该成员自己的 content_hash；任务行上记的是整组的
    指纹，正如 worker 落库时那样。
    """
    import uuid

    from skillprism.db import session_scope
    from skillprism.models import EvaluationResult, EvaluationTask

    task_id = str(uuid.uuid4())
    with session_scope() as session:
        session.add(
            EvaluationTask(
                id=task_id,
                source=source,
                skill_id=BUNDLE_ID,
                # bundle 任务不记登记名：整组物化不用它命名，存下来只会回显
                # 一个没参与计算的值。
                skill_version="v1.2.0",
                content_hash=CONTEXT,
                bundle=True,
                state="done",
            )
        )
        for member, content_hash in members.items():
            session.add(
                EvaluationResult(
                    id=str(uuid.uuid4()),
                    source=source,
                    skill_id=f"{BUNDLE_ID}/{member}",
                    content_hash=content_hash,
                    context_hash=CONTEXT,
                    status="passed",
                    severity_counts={},
                    incomplete_scans=[],
                )
            )
    return task_id


def test_a_bundle_task_hands_back_every_member_key(client):
    """bundle 评完之后，调用方必须能只靠任务接口走到每一条结论。

    成员的 content_hash 原本在任务侧一个出口都没有：既不在任务 DTO 里，也
    没有按任务列成员的接口。调用方只剩两条"知道内情才能用"的通道——自己拼
    ``<bundle_id>/<成员>``，再挨个猜 hash。
    """
    task_id = _seed_bundle_task({"code-review": "hash-cr", "test-gen": "hash-tg"})

    task = client.get(f"/api/tasks/{task_id}").json()

    assert task["bundle"] is True
    assert {(r["skill_id"], r["content_hash"]) for r in task["results"]} == {
        (f"{BUNDLE_ID}/code-review", "hash-cr"),
        (f"{BUNDLE_ID}/test-gen", "hash-tg"),
    }
    # 交回来的键必须真的查得到——这条链路通不通就是本节的全部意义。
    # 键含上下文：同一个 skill 单独评过时，(skill_id, content_hash) 会撞上，
    # 原样回传 context_hash 才取得到确定的那条。
    for ref in task["results"]:
        assert ref["context_hash"] == task["context_hash"]
        got = client.get(
            f"/api/skills/{ref['skill_id']}/evaluation",
            params={
                "source": task["source"],
                "content_hash": ref["content_hash"],
                "context_hash": ref["context_hash"],
            },
        )
        assert got.status_code == 200, got.json()
        assert got.json()["context_hash"] == task["context_hash"]


def test_context_hash_picks_between_two_verdicts_on_one_key(client):
    """同一个 skill 单独评过、又在一组里评过：两条结论共用 (skill_id, content_hash)。

    它们本来就该同时存在（上下文是身份的一部分），所以查询必须有办法指定要
    哪一条。空值不能当成"没给"——单独评的那条上下文就是空的，那样它就永远
    指不到了。
    """
    import datetime as dt
    import uuid

    from skillprism.db import session_scope
    from skillprism.models import EvaluationResult

    skill_id = f"{BUNDLE_ID}/code-review"
    rows = (
        (None, 79.5, dt.datetime(2026, 9, 1)),
        (CONTEXT, 91.0, dt.datetime(2026, 9, 2)),
    )
    with session_scope() as session:
        for context_hash, score, when in rows:
            session.add(
                EvaluationResult(
                    id=str(uuid.uuid4()),
                    source="local",
                    skill_id=skill_id,
                    content_hash="hash-cr",
                    context_hash=context_hash,
                    status="passed",
                    score=score,
                    evaluated_at=when,
                    severity_counts={},
                    incomplete_scans=[],
                )
            )

    def _get(**params):
        return client.get(
            f"/api/skills/{skill_id}/evaluation",
            params={"source": "local", "content_hash": "hash-cr", **params},
        ).json()

    # 空值 = "要单独评的那条"，不是"没给"。
    assert _get(context_hash="")["score"] == 79.5
    assert _get(context_hash=CONTEXT)["score"] == 91.0
    # 完全不带这个参数才是"不限"，回最近评完的那条——补上下文之前发出去的
    # 链接走这条。给"随便一条"不行：那会随数据库的物理顺序变。
    assert _get()["score"] == 91.0


def test_a_bundle_task_does_not_call_the_group_hash_a_content_hash(client):
    """整组指纹不是任何一条结论的寻址键，就不能挂在 content_hash 这个名字下。

    一词之差正是这个坑的全部成因：字段叫 content_hash，调用方就当它是查
    结论的那个 hash——查一辈子都是 404，中间没有一步报错。
    """
    task_id = _seed_bundle_task({"code-review": "hash-cr"})

    task = client.get(f"/api/tasks/{task_id}").json()

    assert task["content_hash"] is None
    assert task["context_hash"] == CONTEXT
    # 与成员结论里的同名字段是同一个值，对账用得上。
    assert task["results"][0]["content_hash"] == "hash-cr"


def test_a_solo_task_addresses_its_one_result_the_same_way(client, public_domain, tmp_path):
    """单任务也给 results，两种形态一个查法。

    调用方不必为 bundle 写第二条代码路径——那正是当初漏掉成员寻址键时，
    对方被迫要写的东西。
    """
    import uuid

    from skillprism.db import session_scope
    from skillprism.models import EvaluationTask

    skill_id = "group/repo:skills/log-triage"
    _seed_two_versions(skill_id, tmp_path / "reports")
    task_id = str(uuid.uuid4())
    with session_scope() as session:
        session.add(
            EvaluationTask(
                id=task_id,
                source="local",
                skill_id=skill_id,
                skill_name="log-triage",
                content_hash="hash-v1",
                state="done",
            )
        )

    task = client.get(f"/api/tasks/{task_id}").json()

    assert task["bundle"] is False
    # 单任务上这个字段本来就是结论的寻址键，语义不变。
    assert task["content_hash"] == "hash-v1"
    assert task["context_hash"] is None
    assert task["results"] == [
        {
            "skill_id": skill_id,
            "content_hash": "hash-v1",
            "context_hash": None,
            "status": "passed",
            "report_url": (
                f"{PUBLIC_BASE_URL}/api/skills/{skill_id}"
                "/report?source=local&content_hash=hash-v1&context_hash="
            ),
        }
    ]


def test_a_queued_task_has_no_results_yet(client):
    """还没下载内容就没有 hash，也就没有结论。空列表，不编占位。"""
    task_id = client.post("/api/evaluations", json=TRIGGER).json()["task_id"]

    task = client.get(f"/api/tasks/{task_id}").json()

    assert task["content_hash"] is None and task["results"] == []


def test_the_group_hash_used_as_a_content_hash_says_so(client):
    """拿组指纹当 content_hash 查时，404 要点破它是什么。

    不点破的话症状是"任务成功、查结论永远 404"，对接时两种情况都会往
    "是不是没触发"上查——而真正的原因是传参传串了一个词。
    """
    _seed_bundle_task({"code-review": "hash-cr", "test-gen": "hash-tg"})

    resp = client.get(
        f"/api/skills/{BUNDLE_ID}/evaluation", params={"content_hash": CONTEXT}
    )

    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert "context_hash" in detail
    assert f"{BUNDLE_ID}/code-review" in detail


def test_an_unknown_hash_is_still_just_unknown(client):
    """认不出的 hash 不能也说成"你拿的是组指纹"——那是猜，会把人带偏。"""
    _seed_bundle_task({"code-review": "hash-cr"})

    resp = client.get(
        f"/api/skills/{BUNDLE_ID}/evaluation", params={"content_hash": "sha256:nope"}
    )

    assert resp.status_code == 404
    assert "context_hash" not in resp.json()["detail"]
