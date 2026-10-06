"""碳排放整改工单 API 专项：认证边界、状态流转、结果回写与越权审计留痕。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import app
from app.models import Company, User


@pytest.fixture()
def ctx():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False)
    Base.metadata.create_all(engine)

    db = TestingSession()
    pwd_hash, salt = hash_password("123456")
    c1 = Company(code="RA-001", name="整改企业甲", industry="电力", region="华东")
    c2 = Company(code="RA-002", name="整改企业乙", industry="水泥", region="华北")
    db.add_all([c1, c2])
    db.flush()
    users = [
        User(username="admin", display_name="监管", role="admin",
             password_hash=pwd_hash, salt=salt),
        User(username="verifier", display_name="核查员", role="verifier",
             password_hash=pwd_hash, salt=salt),
        User(username="ent1", display_name="企业甲", role="enterprise",
             company_id=c1.id, password_hash=pwd_hash, salt=salt),
        User(username="ent2", display_name="企业乙", role="enterprise",
             company_id=c2.id, password_hash=pwd_hash, salt=salt),
    ]
    db.add_all(users)
    db.commit()
    ids = {"c1": c1.id, "c2": c2.id}
    db.close()

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield client, ids
    app.dependency_overrides.clear()
    engine.dispose()


def login(client, username):
    res = client.post("/api/auth/login", json={"username": username, "password": "123456"})
    assert res.status_code == 200


def _create(client, company_id, title="API 整改工单", **extra):
    body = {
        "company_id": company_id, "year": 2026, "title": title,
        "issue_type": "activity_data", "description": "问题描述", "requirement": "整改要求",
        "due_date": "2026-12-01",
    }
    body.update(extra)
    return client.post("/api/rectifications", json=body)


def _evidence_body(name="佐证.pdf"):
    return {
        "evidence_type": "supporting_doc", "file_name": name,
        "file_url": "https://files.example/x", "file_hash": "b" * 64,
        "file_size": 2048, "description": "台账佐证",
    }


# ---------- 认证 / 建单 ----------

def test_list_requires_login(ctx):
    client, _ = ctx
    assert client.get("/api/rectifications").status_code == 401


def test_admin_create_and_list(ctx):
    client, ids = ctx
    login(client, "admin")
    res = _create(client, ids["c1"])
    assert res.status_code == 200
    assert res.json()["status"] == "open"
    assert res.json()["order_no"].startswith("ZG")

    listed = client.get("/api/rectifications").json()
    assert len(listed) == 1 and listed[0]["title"] == "API 整改工单"


def test_enterprise_create_self_and_isolation(ctx):
    client, ids = ctx
    login(client, "ent1")
    assert _create(client, ids["c1"]).status_code == 200
    # 为他企业建单：403 且写越权审计
    denied = _create(client, ids["c2"], title="越权建单")
    assert denied.status_code == 403


def test_create_idempotent(ctx):
    client, ids = ctx
    login(client, "admin")
    h = {"Idempotency-Key": "idem-api-1"}
    r1 = client.post("/api/rectifications",
                     json={"company_id": ids["c1"], "year": 2026, "title": "幂等工单"},
                     headers=h)
    r2 = client.post("/api/rectifications",
                     json={"company_id": ids["c1"], "year": 2026, "title": "幂等工单"},
                     headers=h)
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]
    assert len(client.get("/api/rectifications").json()) == 1


# ---------- 企业整改：证据 + 提交 ----------

def _open_order(client, ids, creator="admin"):
    login(client, creator)
    oid = _create(client, ids["c1"]).json()["id"]
    login(client, "ent1")
    return oid


def test_evidence_and_submit_flow(ctx):
    client, ids = ctx
    oid = _open_order(client, ids)

    # 提交前必须有证据
    no_ev = client.post(f"/api/rectifications/{oid}/submit",
                        json={"summary": "无证据提交"})
    assert no_ev.status_code == 400 and "证据" in no_ev.json()["detail"]

    ev = client.post(f"/api/rectifications/{oid}/evidences", json=_evidence_body())
    assert ev.status_code == 200
    assert ev.json()["file_hash"] == "b" * 64

    submit = client.post(f"/api/rectifications/{oid}/submit",
                         json={"summary": "已完成整改", "measures": "补台账", "impact": "无"})
    assert submit.status_code == 200
    assert submit.json()["status"] == "submitted"
    assert len(submit.json()["evidences"]) == 1


def test_submit_cross_company_forbidden_and_audited(ctx):
    client, ids = ctx
    oid = _open_order(client, ids)
    login(client, "ent2")
    res = client.post(f"/api/rectifications/{oid}/evidences", json=_evidence_body())
    assert res.status_code == 403
    # 越权拒绝独立落审计（监管全量日志可见 denied 记录）
    login(client, "admin")
    logs = client.get("/api/rectifications/audit-logs/all").json()
    assert any(l["result"] == "denied" and l["action"] == "evidence.upload" for l in logs)


def test_cross_company_detail_forbidden(ctx):
    client, ids = ctx
    oid = _open_order(client, ids)
    login(client, "ent2")
    assert client.get(f"/api/rectifications/{oid}").status_code == 403


# ---------- 核查：通过 / 驳回 / 关闭 ----------

def _submitted_order(client, ids):
    oid = _open_order(client, ids)
    client.post(f"/api/rectifications/{oid}/evidences", json=_evidence_body())
    client.post(f"/api/rectifications/{oid}/submit",
                json={"summary": "整改完成", "measures": "x", "impact": "无"})
    return oid


def test_approve_reject_close_permissions(ctx):
    client, ids = ctx
    oid = _submitted_order(client, ids)

    # 企业不能审核
    assert client.post(f"/api/rectifications/{oid}/approve", json={}).status_code == 403

    login(client, "verifier")
    # 驳回需原因
    bad = client.post(f"/api/rectifications/{oid}/reject", json={"reason": "x"})
    assert bad.status_code == 422
    rej = client.post(f"/api/rectifications/{oid}/reject", json={"reason": "材料不完整"})
    assert rej.status_code == 200 and rej.json()["status"] == "open"
    assert rej.json()["rejection_count"] == 1

    # 重新提交
    login(client, "ent1")
    client.post(f"/api/rectifications/{oid}/evidences", json=_evidence_body("补充.pdf"))
    client.post(f"/api/rectifications/{oid}/submit",
                json={"summary": "已补全", "measures": "x", "impact": "无"})

    login(client, "verifier")
    appr = client.post(f"/api/rectifications/{oid}/approve",
                       json={"comment": "通过", "recalculate": False, "followup_reconcile": False})
    assert appr.status_code == 200
    body = appr.json()
    assert body["order"]["status"] == "approved"
    assert body["writeback"]["review_comment"] == "通过"

    # 终态不可再关/再驳回
    assert client.post(f"/api/rectifications/{oid}/close",
                       json={"reason": "关闭"}).status_code == 400


def test_close_with_reason(ctx):
    client, ids = ctx
    login(client, "admin")
    oid = _create(client, ids["c1"], title="待关闭工单").json()["id"]
    res = client.post(f"/api/rectifications/{oid}/close", json={"reason": "问题不成立，关闭"})
    assert res.status_code == 200 and res.json()["status"] == "closed"


def test_approve_writes_report_note(ctx):
    """审核通过回写履约（MRV）报告整改附注（接口侧冒烟）。"""
    client, ids = ctx
    oid = _submitted_order(client, ids)
    login(client, "verifier")
    appr = client.post(f"/api/rectifications/{oid}/approve",
                       json={"recalculate": True, "followup_reconcile": False})
    assert appr.status_code == 200
    wb = appr.json()["writeback"]
    # 无 MRV 报告时 report_annotated 为 False，不报错
    assert "recalculated" in wb


def test_approve_triggers_followup_reconciliation(ctx):
    client, ids = ctx
    oid = _submitted_order(client, ids)
    login(client, "verifier")
    res = client.post(f"/api/rectifications/{oid}/approve",
                      json={"recalculate": False, "followup_reconcile": True})
    assert res.status_code == 200
    assert res.json()["followup_recon_status"] == "balanced"
    assert res.json()["order"]["followup_recon_run_id"]


# ---------- 审计 ----------

def test_audit_logs_regulator_only(ctx):
    client, ids = ctx
    oid = _submitted_order(client, ids)
    login(client, "verifier")
    logs = client.get(f"/api/rectifications/audit-logs?order_id={oid}").json()
    actions = {l["action"] for l in logs}
    assert {"order.create", "evidence.upload", "order.submit"} <= actions

    # 企业越权读审计 403
    login(client, "ent1")
    assert client.get("/api/rectifications/audit-logs?order_id=" + str(oid)).status_code == 403


def test_denied_attempts_persisted(ctx):
    """越权拒绝独立提交审计日志。"""
    client, ids = ctx
    login(client, "ent2")
    # ent2 为 c1 建单被拒
    r = _create(client, ids["c1"], title="越权")
    assert r.status_code == 403
    login(client, "admin")
    logs = client.get("/api/rectifications/audit-logs/all").json()
    denied = [l for l in logs if l["result"] == "denied"]
    assert any(l["action"] == "order.create" for l in denied)


def test_resolution_endpoint_isolation(ctx):
    client, ids = ctx
    login(client, "admin")
    items = client.get("/api/rectifications/discrepancy-resolutions/list")
    assert items.status_code == 200 and items.json() == []
    login(client, "ent1")
    assert client.get("/api/rectifications/discrepancy-resolutions/list").status_code == 200
