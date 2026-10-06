"""碳排放整改工单服务层测试。

覆盖：
- 开单（手动/对账差异/报告/活动数据来源校验、跨企业跨年度拒绝、差异 code 校验）；
- 企业提交整改（仅本企业、状态约束、证据至少一条、轮次递增、历史证据保留）；
- 核查员审核（通过→approved、驳回→open 待整改可重交、状态非法拒绝）；
- 审核通过自动回写：履约报告 report_json 追加整改结论、企业年度对账挂接工单；
- 监管关闭（非终态可关、终态拒绝、原因校验）；
- 审计日志覆盖全部敏感动作；
- 开单幂等键去重。
"""

import json

import pytest

from app.core.security import hash_password
from app.models import (
    ActivityData,
    Company,
    EmissionScope,
    MrvReport,
    RectificationAuditLog,
    RectificationOrder,
    User,
)
from app.models.ledger import LedgerReconciliation
from app.services.mrv_service import generate_report
from app.services.quota_service import allocate_quota
from app.services.reconciliation_service import run_reconciliation
from app.services.rectification_service import (
    RectificationError,
    close_order,
    create_order,
    list_audit_logs,
    list_evidences,
    submit_rectification,
    review_order,
)


@pytest.fixture()
def ctx(db):
    pwd, salt = hash_password("123456")
    c1 = Company(code="R-001", name="整改企业", industry="电力", region="华东")
    c2 = Company(code="R-002", name="其他企业", industry="水泥", region="华北")
    db.add_all([c1, c2])
    db.flush()
    admin = User(username="admin", display_name="监管", role="admin",
                 password_hash=pwd, salt=salt)
    verifier = User(username="verifier", display_name="核查员", role="verifier",
                    password_hash=pwd, salt=salt)
    ent = User(username="e1", display_name="企业员", role="enterprise",
               company_id=c1.id, password_hash=pwd, salt=salt)
    db.add_all([admin, verifier, ent])
    db.commit()

    class Ops:
        admin_id = admin.id
        verifier_id = verifier.id
        ent_id = ent.id

    from app.services.rectification_service import Operator
    ops = Ops()
    ops.admin = Operator(id=admin.id, username="admin", role="admin")
    ops.verifier = Operator(id=verifier.id, username="verifier", role="verifier")
    ops.ent = Operator(id=ent.id, username="e1", role="enterprise")
    return {"db": db, "c1": c1, "c2": c2, "ops": ops}


YEAR = 2025


def _evidences(n=1, name="证据材料"):
    return [{"evidence_type": "document", "name": f"{name}{i}", "file_url": "", "remark": ""}
            for i in range(n)]


# --------------------------------------------------------------------------- #
# 开单
# --------------------------------------------------------------------------- #

def test_create_manual_order(ctx):
    db, c1, ops = ctx["db"], ctx["c1"], ctx["ops"]
    o = create_order(db, company_id=c1.id, year=YEAR, title="核查发现问题",
                     description="活动数据错报", operator=ops.admin,
                     source_type="manual", due_date=f"{YEAR}-12-31")
    assert o.status == "open"
    assert o.order_no.startswith(f"RC{YEAR}")
    assert o.submit_count == 0
    log = db.query(RectificationAuditLog).filter_by(action="order.create").one()
    assert log.result == "success" and log.operator_id == ops.admin_id


def test_create_order_unknown_company(ctx):
    db, ops = ctx["db"], ctx["ops"]
    with pytest.raises(RectificationError, match="企业不存在"):
        create_order(db, company_id=99999, year=YEAR, title="t", description="d",
                     operator=ops.admin)


def test_create_order_bad_due_date(ctx):
    db, c1, ops = ctx["db"], ctx["c1"], ctx["ops"]
    with pytest.raises(RectificationError, match="整改期限"):
        create_order(db, company_id=c1.id, year=YEAR, title="t", description="d",
                     operator=ops.admin, due_date="2025/12/31")


def test_create_order_idempotency(ctx):
    db, c1, ops = ctx["db"], ctx["c1"], ctx["ops"]
    kw = dict(company_id=c1.id, year=YEAR, title="幂等开单", description="重复提交",
              operator=ops.admin)
    o1 = create_order(db, idempotency_key="idem-1", **kw)
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        create_order(db, idempotency_key="idem-1", **kw)
    db.rollback()
    assert db.query(RectificationOrder).filter_by(id=o1.id).count() == 1


def test_create_from_report_validates_ownership(ctx):
    db, c1, c2, ops = ctx["db"], ctx["c1"], ctx["c2"], ctx["ops"]
    scope = EmissionScope(company_id=c1.id, scope="2", category="外购电", name="厂区用电")
    db.add(scope)
    db.commit()
    report = generate_report(db, c1.id, YEAR)

    # 跨企业开单拒绝
    with pytest.raises(RectificationError, match="不属于该企业"):
        create_order(db, company_id=c2.id, year=YEAR, title="t", description="d",
                     operator=ops.verifier, source_type="report", report_id=report.id)
    # 跨年度拒绝
    with pytest.raises(RectificationError, match="不属于该企业"):
        create_order(db, company_id=c1.id, year=YEAR + 1, title="t", description="d",
                     operator=ops.verifier, source_type="report", report_id=report.id)
    # 正常开单
    o = create_order(db, company_id=c1.id, year=YEAR, title="报告快照问题",
                     description="重新核查", operator=ops.verifier,
                     source_type="report", report_id=report.id)
    assert o.report_id == report.id


def test_create_from_activity_validates(ctx):
    db, c1, ops = ctx["db"], ctx["c1"], ctx["ops"]
    scope = EmissionScope(company_id=c1.id, scope="2", category="电", name="用电")
    db.add(scope); db.flush()
    act = ActivityData(company_id=c1.id, scope_id=scope.id, year=YEAR, period="monthly",
                       activity_type="外购电力", unit="MWh", quantity=100, verified=0)
    db.add(act); db.commit()
    with pytest.raises(RectificationError, match="活动数据不属于"):
        create_order(db, company_id=c1.id, year=YEAR + 1, title="t", description="d",
                     operator=ops.admin, source_type="activity", activity_id=act.id)
    o = create_order(db, company_id=c1.id, year=YEAR, title="未核验数据",
                     description="请核实", operator=ops.admin,
                     source_type="activity", activity_id=act.id)
    assert o.activity_id == act.id


def test_create_from_reconciliation_validates(ctx):
    db, c1, c2, ops = ctx["db"], ctx["c1"], ctx["c2"], ctx["ops"]
    allocate_quota(db, c1.id, YEAR, baseline=100, allocation_amount=100)
    run = run_reconciliation(db, scope="company", company_id=c1.id, year=YEAR)
    assert run.status == "balanced"
    # 跨企业
    with pytest.raises(RectificationError, match="不属于该企业"):
        create_order(db, company_id=c2.id, year=YEAR, title="t", description="d",
                     operator=ops.admin, source_type="reconciliation",
                     reconciliation_id=run.id)
    # balanced 运行无差异，虚构 code 被拒
    with pytest.raises(RectificationError, match="差异代码"):
        create_order(db, company_id=c1.id, year=YEAR, title="t", description="d",
                     operator=ops.admin, source_type="reconciliation",
                     reconciliation_id=run.id, discrepancy_codes=["CHAIN_SEQ_GAP"])
    # 无 discrepancy_codes 可正常开单
    o = create_order(db, company_id=c1.id, year=YEAR, title="例行复核",
                     description="对账后跟踪", operator=ops.admin,
                     source_type="reconciliation", reconciliation_id=run.id)
    assert o.reconciliation_id == run.id


def test_create_from_reconciliation_missing_ref(ctx):
    db, c1, ops = ctx["db"], ctx["c1"], ctx["ops"]
    with pytest.raises(RectificationError, match="必须指定对账运行记录"):
        create_order(db, company_id=c1.id, year=YEAR, title="t", description="d",
                     operator=ops.admin, source_type="reconciliation")


# --------------------------------------------------------------------------- #
# 企业提交整改
# --------------------------------------------------------------------------- #

def _open_order(ctx, **kw):
    db, c1, ops = ctx["db"], ctx["c1"], ctx["ops"]
    defaults = dict(company_id=c1.id, year=YEAR, title="问题", description="描述",
                    operator=ops.admin)
    defaults.update(kw)
    return create_order(db, **defaults)


def test_submit_rectification_happy_path(ctx):
    db, c1, ops = ctx["db"], ctx["c1"], ctx["ops"]
    o = _open_order(ctx)
    o = submit_rectification(db, o.id, company_id=c1.id, operator=ops.ent,
                             rectification_measure="更正台账并重新核算",
                             emission_adjustment=50.0, evidences=_evidences(2))
    assert o.status == "submitted"
    assert o.submit_count == 1
    assert float(o.emission_adjustment) == 50.0
    evs = list_evidences(db, o.id)
    assert len(evs) == 2 and all(e.round == 1 for e in evs)


def test_submit_other_company_forbidden(ctx):
    db, c2, ops = ctx["db"], ctx["c2"], ctx["ops"]
    o = _open_order(ctx)
    with pytest.raises(RectificationError, match="无权整改其他企业"):
        submit_rectification(db, o.id, company_id=c2.id, operator=ops.ent,
                             rectification_measure="x" * 5, evidences=_evidences())


def test_submit_requires_evidence_and_measure(ctx):
    db, c1, ops = ctx["db"], ctx["c1"], ctx["ops"]
    o = _open_order(ctx)
    with pytest.raises(RectificationError, match="整改措施"):
        submit_rectification(db, o.id, company_id=c1.id, operator=ops.ent,
                             rectification_measure="x", evidences=_evidences())
    with pytest.raises(RectificationError, match="证据"):
        submit_rectification(db, o.id, company_id=c1.id, operator=ops.ent,
                             rectification_measure="有效措施", evidences=[])


def test_double_submit_while_pending_review_rejected(ctx):
    db, c1, ops = ctx["db"], ctx["c1"], ctx["ops"]
    o = _open_order(ctx)
    submit_rectification(db, o.id, company_id=c1.id, operator=ops.ent,
                         rectification_measure="措施内容", evidences=_evidences())
    with pytest.raises(RectificationError, match="等待核查员审核"):
        submit_rectification(db, o.id, company_id=c1.id, operator=ops.ent,
                             rectification_measure="再次提交", evidences=_evidences())


# --------------------------------------------------------------------------- #
# 审核：驳回 / 通过 / 回写
# --------------------------------------------------------------------------- #

def _submitted_order(ctx, report=False):
    db, c1, ops = ctx["db"], ctx["c1"], ctx["ops"]
    kw = {}
    if report:
        r = generate_report(db, c1.id, YEAR)
        kw = dict(source_type="report", report_id=r.id)
    o = _open_order(ctx, **kw)
    o = submit_rectification(db, o.id, company_id=c1.id, operator=ops.ent,
                             rectification_measure="完成数据更正",
                             emission_adjustment=12.5, evidences=_evidences())
    return o


def test_review_only_submitted(ctx):
    db, ops = ctx["db"], ctx["ops"]
    o = _open_order(ctx)
    with pytest.raises(RectificationError, match="待审核"):
        review_order(db, o.id, operator=ops.verifier, approved=True, comment="通过意见")


def test_review_requires_comment(ctx):
    db, ops = ctx["db"], ctx["ops"]
    o = _submitted_order(ctx)
    with pytest.raises(RectificationError, match="审核意见"):
        review_order(db, o.id, operator=ops.verifier, approved=False, comment="x")


def test_reject_then_resubmit_keeps_evidence_history(ctx):
    db, c1, ops = ctx["db"], ctx["c1"], ctx["ops"]
    o = _submitted_order(ctx)
    o = review_order(db, o.id, operator=ops.verifier, approved=False,
                     comment="证据不足，请补原始台账")
    assert o.status == "rejected"
    assert o.review_comment == "证据不足，请补原始台账"

    o = submit_rectification(db, o.id, company_id=c1.id, operator=ops.ent,
                             rectification_measure="补充原始抄表记录",
                             evidences=[{"evidence_type": "data", "name": "抄表包",
                                         "file_url": "", "remark": ""}])
    assert o.status == "submitted" and o.submit_count == 2
    evs = list_evidences(db, o.id)
    assert [e.round for e in evs] == [1, 2]  # 历史证据保留，轮次递增


def test_approve_writes_back_report_and_runs_reconciliation(ctx):
    db, c1, ops = ctx["db"], ctx["c1"], ctx["ops"]
    allocate_quota(db, c1.id, YEAR, baseline=100, allocation_amount=100)
    o = _submitted_order(ctx, report=True)
    report_id = o.report_id

    o = review_order(db, o.id, operator=ops.verifier, approved=True,
                     comment="整改材料完整，予以通过")
    assert o.status == "approved"
    # 认定排放调整量缺省取企业申报值
    assert float(o.confirmed_emission_adjustment) == 12.5

    # 履约报告回写：report_json.rectifications 追加一条
    report = db.get(MrvReport, report_id)
    detail = json.loads(report.report_json)
    notes = detail.get("rectifications", [])
    assert len(notes) == 1
    assert notes[0]["rectification_order_id"] == o.id
    assert notes[0]["confirmed_emission_adjustment"] == 12.5

    # 对账回写：挂接一条 balanced 的企业年度运行
    assert o.writeback_reconciliation_id is not None
    run = db.get(LedgerReconciliation, o.writeback_reconciliation_id)
    assert run.scope == "company" and run.company_id == c1.id and run.year == YEAR
    assert o.writeback_status == run.status == "balanced"
    assert o.writeback_discrepancy_count == 0

    # 审核/回写均留审计
    actions = {x.action for x in list_audit_logs(db, order_id=o.id)}
    assert {"order.create", "order.submit", "review.approve", "review.writeback"} <= actions


def test_approve_confirmed_adjustment_overrides(ctx):
    db, ops = ctx["db"], ctx["ops"]
    o = _submitted_order(ctx)
    o = review_order(db, o.id, operator=ops.admin, approved=True, comment="通过",
                     confirmed_emission_adjustment=-3.0)
    assert float(o.confirmed_emission_adjustment) == -3.0


def test_cannot_review_or_submit_terminal(ctx):
    db, c1, ops = ctx["db"], ctx["c1"], ctx["ops"]
    o = _submitted_order(ctx)
    review_order(db, o.id, operator=ops.verifier, approved=True, comment="通过")
    with pytest.raises(RectificationError, match="已终结"):
        submit_rectification(db, o.id, company_id=c1.id, operator=ops.ent,
                             rectification_measure="再改", evidences=_evidences())
    with pytest.raises(RectificationError, match="待审核"):
        review_order(db, o.id, operator=ops.verifier, approved=True, comment="再审")


# --------------------------------------------------------------------------- #
# 关闭
# --------------------------------------------------------------------------- #

def test_close_open_order(ctx):
    db, ops = ctx["db"], ctx["ops"]
    o = _open_order(ctx)
    o = close_order(db, o.id, operator=ops.admin, reason="与其他工单重复")
    assert o.status == "closed" and o.close_reason == "与其他工单重复"
    assert o.closed_by == ops.admin_id and o.closed_at is not None


def test_close_rejected_order(ctx):
    db, ops = ctx["db"], ctx["ops"]
    o = _submitted_order(ctx)
    review_order(db, o.id, operator=ops.verifier, approved=False, comment="先驳回")
    o = close_order(db, o.id, operator=ops.admin, reason="企业已停产，终止整改")
    assert o.status == "closed"


def test_close_terminal_rejected(ctx):
    db, ops = ctx["db"], ctx["ops"]
    o = _submitted_order(ctx)
    review_order(db, o.id, operator=ops.verifier, approved=True, comment="通过")
    with pytest.raises(RectificationError, match="已终结"):
        close_order(db, o.id, operator=ops.admin, reason="再关一次")


def test_close_requires_reason(ctx):
    db, ops = ctx["db"], ctx["ops"]
    o = _open_order(ctx)
    with pytest.raises(RectificationError, match="关闭原因"):
        close_order(db, o.id, operator=ops.admin, reason="x")


# --------------------------------------------------------------------------- #
# 审计轨迹
# --------------------------------------------------------------------------- #

def test_full_lifecycle_audit_trail(ctx):
    db, c1, ops = ctx["db"], ctx["c1"], ctx["ops"]
    o = _open_order(ctx)
    submit_rectification(db, o.id, company_id=c1.id, operator=ops.ent,
                         rectification_measure="整改", evidences=_evidences())
    review_order(db, o.id, operator=ops.verifier, approved=False, comment="驳回意见")
    submit_rectification(db, o.id, company_id=c1.id, operator=ops.ent,
                         rectification_measure="再次整改", evidences=_evidences())
    review_order(db, o.id, operator=ops.verifier, approved=True, comment="通过意见")

    logs = list_audit_logs(db, order_id=o.id, limit=100)
    actions = [x.action for x in logs]
    # 时间倒序：最后一条是开单
    assert actions[-1] == "order.create"
    assert actions.count("order.submit") == 2
    assert actions.count("review.reject") == 1
    assert actions.count("review.approve") == 1
    assert all(x.result == "success" for x in logs)
    assert all(x.operator_name for x in logs)
