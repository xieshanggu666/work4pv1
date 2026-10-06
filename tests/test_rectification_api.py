"""碳排放整改工单 API 集成测试。

覆盖：
- 角色边界：仅监管可开单/审核/关闭/读审计；仅企业可提交且只能提交本企业工单；
- 越权操作 403 且写 rectification_audit_logs（result=denied）；
- 列表企业隔离、详情越权 403、未登录 401；
- 端到端流转：开单 → 提交（证据）→ 驳回 → 重新提交 → 通过（自动对账回写）；
- 关闭流程、来源单据 400 校验、审核通过后重跑对账回写。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.event_hooks import install_ledger_hooks
from app.core.security import hash_password
from app.main import app
from app.models import Company, RectificationAuditLog, User
from app.services.quota_service import allocate_quota

YEAR = 2027


def _seed_db(TestingSession):
    db = TestingSession()
    pwd, salt = hash_password("123456")
    c1 = Company(code="RC-001", name="整改企业", industry="电力", region="华东")
    c2 = Company(code="RC-002", name="无关企业", industry="水泥", region="华北")
    db.add_all([c1, c2])
    db.flush()
    db.add_all([
        User(username="e1", display_name="整改企业员", role="enterprise",
             company_id=c1.id, password_hash=pwd, salt=salt),
        User(username="e2", display_name="无关企业员", role="enterprise",
             company_id=c2.id, password_hash=pwd, salt=salt),
        User(username="admin", display_name="监管员", role="admin",
             password_hash=pwd, salt=salt),
        User(username="verifier", display_name="核查员", role="verifier",
             password_hash=pwd, salt=salt),
    ])
    allocate_quota(db, c1.id, YEAR, baseline=1000, allocation_amount=1000)
    db.commit()
    ids = {"c1": c1.id, "c2": c2.id}
    db.close()
    return ids


@pytest.fixture()
def ctx():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False)
    Base.metadata.create_all(engine)
    install_ledger_hooks(TestingSession)

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    ids = _seed_db(TestingSession)
    client = TestClient(app)
    yield client, ids, TestingSession
    app.dependency_overrides.clear()
    engine.dispose()


def login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": "123456"})
    assert r.status_code == 200


def _create(client, ids, **extra):
    body = {
        "company_id": ids["c1"], "year": YEAR,
        "title": "外购电力数据存疑", "description": "活动量与电费结算单不一致，请核实整改",
        "source_type": "manual", "due_date": f"{YEAR}-12-31",
    }
    body.update(extra)
    return client.post("/api/rectifications", json=body)


# --------------------------------------------------------------------------- #
# 认证与权限
# --------------------------------------------------------------------------- #

def test_list_requires_login(ctx):
    client, _, _ = ctx
    assert client.get("/api/rectifications").status_code == 401


def test_enterprise_cannot_create(ctx):
    client, ids, TS = ctx
    login(client, "e1")
    r = _create(client, ids)
    assert r.status_code == 403
    db = TS()
    denied = db.query(RectificationAuditLog).filter_by(action="order.create", result="denied").count()
    assert denied == 1
    db.close()


def test_verifier_can_create(ctx):
    client, ids, _ = ctx
    login(client, "verifier")
    r = _create(client, ids)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "open" and data["source_type"] == "manual"
    assert data["company_name"] == "整改企业"


def test_regulator_cannot_submit(ctx):
    client, ids, TS = ctx
    login(client, "admin")
    r = _create(client, ids)
    oid = r.json()["id"]
    r = client.post(f"/api/rectifications/{oid}/submit", json={
        "rectification_measure": "监管代填整改措施",
        "evidences": [{"evidence_type": "document", "name": "材料"}],
    })
    assert r.status_code == 403
    db = TS()
    assert db.query(RectificationAuditLog).filter_by(action="order.submit", result="denied").count() == 1
    db.close()


def test_other_enterprise_cannot_submit_or_view(ctx):
    client, ids, _ = ctx
    login(client, "admin")
    oid = _create(client, ids).json()["id"]

    login(client, "e2")
    # 列表隔离：看不到别家工单
    listing = client.get("/api/rectifications").json()
    assert listing == []
    # 详情 403
    assert client.get(f"/api/rectifications/{oid}").status_code == 403
    # 提交别家工单 403 并留痕
    r = client.post(f"/api/rectifications/{oid}/submit", json={
        "rectification_measure": "别家企业代整改",
        "evidences": [{"evidence_type": "document", "name": "材料"}],
    })
    assert r.status_code == 403


def test_enterprise_cannot_review_or_close(ctx):
    client, ids, _ = ctx
    login(client, "verifier")
    oid = _create(client, ids).json()["id"]
    login(client, "e1")
    r = client.post(f"/api/rectifications/{oid}/review",
                    json={"approved": True, "comment": "企业自审通过"})
    assert r.status_code == 403
    r = client.post(f"/api/rectifications/{oid}/close", json={"reason": "企业关闭"})
    assert r.status_code == 403


def test_audit_logs_regulator_only(ctx):
    client, ids, TS = ctx
    login(client, "verifier")
    _create(client, ids)
    assert client.get("/api/rectifications/audit-logs").status_code == 200
    login(client, "e1")
    r = client.get("/api/rectifications/audit-logs")
    assert r.status_code == 403
    db = TS()
    assert db.query(RectificationAuditLog).filter_by(action="audit.read", result="denied").count() == 1
    db.close()


# --------------------------------------------------------------------------- #
# 端到端流转
# --------------------------------------------------------------------------- #

def test_full_flow_create_submit_reject_resubmit_approve(ctx):
    client, ids, _ = ctx

    # 监管开单
    login(client, "admin")
    oid = _create(client, ids).json()["id"]

    # 企业提交
    login(client, "e1")
    r = client.post(f"/api/rectifications/{oid}/submit", json={
        "rectification_measure": "重新核对电表，更正 3 月活动量",
        "emission_adjustment": -35.2,
        "evidences": [
            {"evidence_type": "document", "name": "电费结算单", "file_url": "/bill.pdf"},
            {"evidence_type": "data", "name": "抄表记录", "remark": "逐月"},
        ],
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "submitted" and data["submit_count"] == 1

    # 详情含证据
    detail = client.get(f"/api/rectifications/{oid}").json()
    assert len(detail["evidences"]) == 2

    # 待审核状态重复提交 -> 400
    r = client.post(f"/api/rectifications/{oid}/submit", json={
        "rectification_measure": "重复提交",
        "evidences": [{"evidence_type": "other", "name": "x"}],
    })
    assert r.status_code == 400

    # 核查员驳回（意见太短 422）
    login(client, "verifier")
    r = client.post(f"/api/rectifications/{oid}/review",
                    json={"approved": False, "comment": "x"})
    assert r.status_code == 422
    r = client.post(f"/api/rectifications/{oid}/review",
                    json={"approved": False, "comment": "缺少原始台账，请补充"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"

    # 企业重新提交
    login(client, "e1")
    r = client.post(f"/api/rectifications/{oid}/submit", json={
        "rectification_measure": "补充原始抄表台账并重新核算",
        "evidences": [{"evidence_type": "document", "name": "原始台账扫描件"}],
    })
    assert r.status_code == 200 and r.json()["submit_count"] == 2
    detail = client.get(f"/api/rectifications/{oid}").json()
    assert [e["round"] for e in detail["evidences"]] == [1, 1, 2]

    # 核查员通过：自动回写企业年度对账（balanced）
    login(client, "verifier")
    r = client.post(f"/api/rectifications/{oid}/review",
                    json={"approved": True, "comment": "材料完整，整改有效"})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "approved"
    assert data["writeback_status"] == "balanced"
    assert data["writeback_reconciliation_id"]
    assert data["writeback_discrepancy_count"] == 0

    # 重跑对账回写
    r = client.post(f"/api/rectifications/{oid}/rerun-writeback")
    assert r.status_code == 200 and r.json()["writeback_status"] == "balanced"


def test_submit_validation_evidence_required(ctx):
    client, ids, _ = ctx
    login(client, "admin")
    oid = _create(client, ids).json()["id"]
    login(client, "e1")
    r = client.post(f"/api/rectifications/{oid}/submit", json={
        "rectification_measure": "没有证据的整改", "evidences": [],
    })
    assert r.status_code == 422  # min_length=1


def test_create_from_invalid_reference_400(ctx):
    client, ids, _ = ctx
    login(client, "admin")
    r = _create(client, ids, source_type="reconciliation", reconciliation_id=99999)
    assert r.status_code == 400 and "对账运行记录不存在" in r.json()["detail"]
    r = _create(client, ids, source_type="report", report_id=99999)
    assert r.status_code == 400 and "MRV 报告不存在" in r.json()["detail"]


def test_close_flow(ctx):
    client, ids, _ = ctx
    login(client, "admin")
    oid = _create(client, ids).json()["id"]

    # 原因过短 422
    r = client.post(f"/api/rectifications/{oid}/close", json={"reason": "x"})
    assert r.status_code == 422
    r = client.post(f"/api/rectifications/{oid}/close",
                    json={"reason": "重复开单，合并到其他工单"})
    assert r.status_code == 200 and r.json()["status"] == "closed"
    # 终态再关 400
    r = client.post(f"/api/rectifications/{oid}/close", json={"reason": "再关一次"})
    assert r.status_code == 400


def test_404_missing_order(ctx):
    client, _, _ = ctx
    login(client, "admin")
    assert client.get("/api/rectifications/99999").status_code == 404
    assert client.post("/api/rectifications/99999/close",
                       json={"reason": "关闭原因"}).status_code == 404


def test_list_filters(ctx):
    client, ids, _ = ctx
    login(client, "admin")
    _create(client, ids, title="工单甲")
    oid2 = _create(client, ids, title="工单乙").json()["id"]
    client.post(f"/api/rectifications/{oid2}/close", json={"reason": "无需整改关闭"})

    r = client.get("/api/rectifications", params={"status": "closed"})
    assert [x["title"] for x in r.json()] == ["工单乙"]
    r = client.get("/api/rectifications", params={"source_type": "manual", "year": YEAR})
    assert len(r.json()) == 2
