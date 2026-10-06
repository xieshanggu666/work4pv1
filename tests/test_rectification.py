"""碳排放整改工单服务层专项测试。

覆盖：
- 建单（监管任意企业 / 企业仅本企业 / 越权拒绝）、幂等键去重；
- 证据上传与企业提交（至少一份证据）、终态拦截；
- 核查员审核通过 / 驳回（回 open 可重提）/ 关闭（终态拦截）；
- 审核通过回写履约（MRV）报告整改附注、同事务重算排放量与草稿刷新、
  审核后自动发起企业范围对账复验；
- 对账差异转单：差异指纹固化、通过=resolved、关闭=waived、复发检测、
  同一差异重复转单幂等；
- 监管审计：建单/举证/提交/通过/驳回/关闭全程留痕。
"""

import pytest

from app.models import (
    ActivityData,
    AllowanceAccount,
    Company,
    EmissionScope,
    User,
)
from app.models.emission import EmissionFactor
from app.models.rectification import (
    CarbonRectificationAuditLog,
    CarbonRectificationOrder,
    ReconDiscrepancyResolution,
)
from app.schemas import (
    RectificationEvidenceIn,
    RectificationOrderIn,
    RectificationReviewIn,
    RectificationSubmitIn,
)
from app.services.calculation_service import annual_total, recalc_company_year
from app.services.quota_service import allocate_quota
from app.services.rectification_service import (
    OPEN,
    SUBMITTED,
    APPROVED,
    CLOSED,
    RectificationError,
    add_evidence,
    approve_order,
    close_order,
    create_order,
    list_audit_logs,
    reject_order,
    submit_order,
    write_audit,
    Operator,
)
from app.services.reconciliation_service import run_reconciliation


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

@pytest.fixture()
def ctx(db):
    admin = User(username="admin", display_name="监管", role="admin",
                 password_hash="x", salt="x")
    verifier = User(username="verifier", display_name="核查员", role="verifier",
                    password_hash="x", salt="x")
    c1 = Company(code="R-001", name="整改企业甲", industry="电力", region="华东")
    c2 = Company(code="R-002", name="整改企业乙", industry="水泥", region="华北")
    db.add_all([admin, verifier, c1, c2])
    db.flush()
    ent1_user = User(username="ent1", display_name="企业甲", role="enterprise",
                     company_id=c1.id, password_hash="x", salt="x")
    ent2_user = User(username="ent2", display_name="企业乙", role="enterprise",
                     company_id=c2.id, password_hash="x", salt="x")
    db.add_all([ent1_user, ent2_user])
    db.flush()

    scope = EmissionScope(company_id=c1.id, scope="2", category="外购电力", name="厂区用电")
    db.add(scope)
    # 2026 年生效的外购电力因子（核算按活动类型名称匹配因子）
    db.add(EmissionFactor(
        factor_code="ELEC-GRID-2026", name="外购电力", scope="2", unit="tCO2/MWh",
        value=0.5703, source="电网因子2026", valid_from="2026-01-01", valid_to=None,
    ))
    db.commit()
    return {
        "db": db,
        "admin": Operator(admin.id, "admin", "admin"),
        "verifier": Operator(verifier.id, "verifier", "verifier"),
        "ent1": Operator(ent1_user.id, "ent1", "enterprise", company_id=c1.id),
        "ent2": Operator(ent2_user.id, "ent2", "enterprise", company_id=c2.id),
        "c1": c1,
        "c2": c2,
        "scope": scope,
        "admin_user": admin,
        "verifier_user": verifier,
    }


def _order_in(company_id, **kw):
    base = dict(
        company_id=company_id, year=2026, title="2026年度活动数据缺计量佐证",
        issue_type="activity_data", description="Q3 燃煤消耗无票据", requirement="补齐计量台账",
        due_date="2026-11-30",
    )
    base.update(kw)
    return RectificationOrderIn(**base)


def _evidence(file_name="台账.xlsx", etype="supporting_doc"):
    return RectificationEvidenceIn(
        evidence_type=etype, file_name=file_name,
        file_url="https://files.example/" + file_name,
        file_hash="a" * 64, file_size=1024, description="佐证材料",
    )


def _submit_in(summary="已补齐 Q3 燃煤计量台账并重新核算", with_evidence=True):
    return RectificationSubmitIn(
        summary=summary,
        measures="安装在线计量、按月归档票据",
        impact="核算排放量无变化",
        evidences=[_evidence()] if with_evidence else None,
    )


# --------------------------------------------------------------------------- #
# 建单与权限
# --------------------------------------------------------------------------- #

def test_regulator_create_order(ctx):
    db = ctx["db"]
    order = create_order(db, _order_in(ctx["c1"].id), ctx["admin"])
    assert order.status == OPEN
    assert order.order_no.startswith("ZG")
    assert order.source == "manual"
    assert order.created_by == ctx["admin"].id
    # 审计留痕
    logs = list_audit_logs(db, order_id=order.id)
    assert logs[0].action == "order.create" and logs[0].result == "success"


def test_enterprise_self_create_ok(ctx):
    order = create_order(ctx["db"], _order_in(ctx["c1"].id), ctx["ent1"])
    assert order.company_id == ctx["c1"].id


def test_enterprise_create_for_other_company_denied(ctx):
    with pytest.raises(RectificationError) as ei:
        create_order(ctx["db"], _order_in(ctx["c2"].id), ctx["ent1"])
    assert ei.value.http_status == 403


def test_create_order_idempotent(ctx):
    data = _order_in(ctx["c1"].id, idempotency_key="idem-create-1")
    o1 = create_order(ctx["db"], data, ctx["admin"])
    o2 = create_order(ctx["db"], data, ctx["admin"])
    assert o1.id == o2.id
    assert ctx["db"].query(CarbonRectificationOrder).count() == 1


def test_unknown_company_404(ctx):
    with pytest.raises(RectificationError) as ei:
        create_order(ctx["db"], _order_in(999999), ctx["admin"])
    assert ei.value.http_status == 404


# --------------------------------------------------------------------------- #
# 证据与提交
# --------------------------------------------------------------------------- #

def test_submit_requires_evidence(ctx):
    db = ctx["db"]
    order = create_order(db, _order_in(ctx["c1"].id), ctx["admin"])
    with pytest.raises(RectificationError, match="证据"):
        submit_order(db, order.id, _submit_in(with_evidence=False), ctx["ent1"])


def test_cross_company_evidence_denied(ctx):
    db = ctx["db"]
    order = create_order(db, _order_in(ctx["c1"].id), ctx["admin"])
    with pytest.raises(RectificationError) as ei:
        add_evidence(db, order.id, _evidence(), ctx["ent2"])
    assert ei.value.http_status == 403


def test_evidence_submit_full_flow(ctx):
    db = ctx["db"]
    order = create_order(db, _order_in(ctx["c1"].id), ctx["admin"])
    add_evidence(db, order.id, _evidence("方案.pdf", "rectification_plan"), ctx["ent1"])
    order = submit_order(db, order.id, _submit_in(), ctx["ent1"])
    assert order.status == SUBMITTED
    assert order.submission_json  # 整改说明已落库
    # 先单独上传 1 份，提交时再随附 1 份
    from app.services.rectification_service import list_evidences
    assert len(list_evidences(db, order.id)) == 2


def test_submit_wrong_status_denied(ctx):
    db = ctx["db"]
    order = create_order(db, _order_in(ctx["c1"].id), ctx["admin"])
    add_evidence(db, order.id, _evidence(), ctx["ent1"])
    submit_order(db, order.id, _submit_in(), ctx["ent1"])
    # 已 submitted 不能重复提交
    with pytest.raises(RectificationError, match="待整改"):
        submit_order(db, order.id, _submit_in(), ctx["ent1"])


def test_terminal_order_rejects_evidence(ctx):
    db = ctx["db"]
    order = create_order(db, _order_in(ctx["c1"].id), ctx["admin"])
    add_evidence(db, order.id, _evidence(), ctx["ent1"])
    submit_order(db, order.id, _submit_in(), ctx["ent1"])
    approve_order(db, order.id, RectificationReviewIn(followup_reconcile=False), ctx["verifier"])
    with pytest.raises(RectificationError, match="终结"):
        add_evidence(db, order.id, _evidence("迟到.pdf"), ctx["ent1"])


# --------------------------------------------------------------------------- #
# 审核：驳回 / 通过 / 关闭
# --------------------------------------------------------------------------- #

def test_reject_returns_to_open_and_resubmit(ctx):
    db = ctx["db"]
    order = create_order(db, _order_in(ctx["c1"].id), ctx["admin"])
    add_evidence(db, order.id, _evidence(), ctx["ent1"])
    submit_order(db, order.id, _submit_in(), ctx["ent1"])

    order = reject_order(db, order.id, "票据不清晰，请补盖公章", ctx["verifier"])
    assert order.status == OPEN
    assert order.rejection_count == 1
    assert "公章" in order.reject_reason

    # 企业按驳回意见补证据后重新提交，再通过
    add_evidence(db, order.id, _evidence("补充台账.pdf"), ctx["ent1"])
    order = submit_order(db, order.id, _submit_in("已补盖公章并重新归档"), ctx["ent1"])
    assert order.status == SUBMITTED
    order, result = approve_order(
        db, order.id, RectificationReviewIn(followup_reconcile=False), ctx["verifier"]
    )
    assert order.status == APPROVED


def test_approve_requires_submitted(ctx):
    db = ctx["db"]
    order = create_order(db, _order_in(ctx["c1"].id), ctx["admin"])
    with pytest.raises(RectificationError, match="已提交整改"):
        approve_order(db, order.id, RectificationReviewIn(followup_reconcile=False), ctx["verifier"])


def test_enterprise_cannot_approve(ctx):
    db = ctx["db"]
    order = create_order(db, _order_in(ctx["c1"].id), ctx["admin"])
    with pytest.raises(RectificationError) as ei:
        approve_order(db, order.id, RectificationReviewIn(followup_reconcile=False), ctx["ent1"])
    assert ei.value.http_status == 403


def test_close_requires_reason_and_terminal(ctx):
    db = ctx["db"]
    order = create_order(db, _order_in(ctx["c1"].id), ctx["admin"])
    with pytest.raises(RectificationError, match="关闭原因"):
        close_order(db, order.id, "x", "waived", ctx["admin"])
    order = close_order(db, order.id, "经核查问题不成立", "waived", ctx["admin"])
    assert order.status == CLOSED
    # 终态不可再关
    with pytest.raises(RectificationError, match="终结"):
        close_order(db, order.id, "再次关闭", "waived", ctx["admin"])


def test_reject_requires_reason(ctx):
    db = ctx["db"]
    order = create_order(db, _order_in(ctx["c1"].id), ctx["admin"])
    add_evidence(db, order.id, _evidence(), ctx["ent1"])
    submit_order(db, order.id, _submit_in(), ctx["ent1"])
    with pytest.raises(RectificationError, match="驳回原因"):
        reject_order(db, order.id, "x", ctx["verifier"])


# --------------------------------------------------------------------------- #
# 审核通过回写履约报告 + 同事务重算
# --------------------------------------------------------------------------- #

def _seed_year_with_activity(db, c1, scope, *, verified, quantity=88000.0):
    """构造一条外购电力活动数据（因子在 seed 夹具中已存在 ELEC-GRID=0.5703）。"""
    act = ActivityData(
        company_id=c1.id, scope_id=scope.id, year=2026, period="monthly",
        activity_type="外购电力", unit="MWh", quantity=quantity,
        data_source="电网结算单", verified=1 if verified else 0,
    )
    db.add(act)
    db.commit()
    return act


def test_approve_annotates_report_and_recalculates(ctx):
    db = ctx["db"]
    c1, scope = ctx["c1"], ctx["scope"]
    _seed_year_with_activity(db, c1, scope, verified=True, quantity=100000.0)
    recalc_company_year(db, c1.id, 2026)
    before = annual_total(db, c1.id, 2026)
    assert before > 0

    # 生成一份草稿 MRV 报告（此时快照=旧排放量）
    from app.services.mrv_service import generate_report
    report = generate_report(db, c1.id, 2026)
    assert report.status == "draft"

    order = create_order(db, _order_in(c1.id), ctx["admin"])
    add_evidence(db, order.id, _evidence(), ctx["ent1"])
    submit_order(db, order.id, _submit_in(), ctx["ent1"])

    order, result = approve_order(
        db, order.id,
        RectificationReviewIn(comment="证据充分", recalculate=False, followup_reconcile=False),
        ctx["verifier"],
    )
    # 关闭重算时仅回写报告附注
    db.refresh(report)
    notes = __import__("json").loads(report.rectification_notes_json)
    assert len(notes) == 1 and notes[0]["order_id"] == order.id
    assert result["writeback"]["recalculated"] is False

    # 再来一张工单，开启重算：修改活动量后审核通过，草稿被刷新
    act = db.query(ActivityData).filter_by(company_id=c1.id, year=2026).one()
    act.quantity = 120000
    db.commit()

    order2 = create_order(db, _order_in(c1.id, title="第二次整改：电量更新"), ctx["admin"])
    add_evidence(db, order2.id, _evidence("新台账.xlsx"), ctx["ent1"])
    submit_order(db, order2.id, _submit_in("已按新电量重新核算"), ctx["ent1"])
    order2, result2 = approve_order(
        db, order2.id,
        RectificationReviewIn(recalculate=True, followup_reconcile=False),
        ctx["verifier"],
    )
    wb = result2["writeback"]
    assert wb["recalculated"] is True
    assert wb["emission_after"] > wb["emission_before"]
    db.refresh(report)
    assert abs(float(report.total_emission) - wb["emission_after"]) < 0.01
    assert report.status == "draft"
    notes = __import__("json").loads(report.rectification_notes_json)
    assert len(notes) == 2  # 附注只追加


def test_approve_with_followup_reconciliation(ctx):
    db = ctx["db"]
    order = create_order(db, _order_in(ctx["c1"].id), ctx["admin"])
    add_evidence(db, order.id, _evidence(), ctx["ent1"])
    submit_order(db, order.id, _submit_in(), ctx["ent1"])
    order, result = approve_order(
        db, order.id,
        RectificationReviewIn(recalculate=False, followup_reconcile=True),
        ctx["verifier"],
    )
    assert order.followup_recon_run_id is not None
    assert result["followup_recon_status"] in ("balanced", "discrepancy")
    wb = result["writeback"]
    assert wb["followup_recon_run_id"] == order.followup_recon_run_id
    assert wb["discrepancy_still_present"] is False  # 手工工单无关联差异


# --------------------------------------------------------------------------- #
# 对账差异转单 → 指纹 → resolved/waived → 复发检测
# --------------------------------------------------------------------------- #

def _make_tampered_run(db, company):
    """制造一个真实差异（库外篡改账户余额）并运行企业范围对账。"""
    allocate_quota(db, company.id, 2026, baseline=1000, allocation_amount=1000)
    account = db.query(AllowanceAccount).filter_by(company_id=company.id, year=2026).one()
    # 库外篡改：直接改余额但不留流水，重放投影必然不符
    account.current_balance = float(account.current_balance) + 50
    db.commit()
    run = run_reconciliation(db, scope="company", company_id=company.id, year=2026)
    assert run.status == "discrepancy"
    return run


def test_discrepancy_to_order_resolved_and_recurrence(ctx):
    db = ctx["db"]
    c1 = ctx["c1"]
    run = _make_tampered_run(db, c1)
    issues = __import__("json").loads(run.discrepancies_json)
    codes = [i["code"] for i in issues]
    assert "PROJECTION_CURRENT_MISMATCH" in codes
    idx = codes.index("PROJECTION_CURRENT_MISMATCH")

    data = RectificationOrderIn(
        company_id=c1.id, year=2026, title="对账差异整改：账实不符",
        issue_type="reconciliation",
        recon_run_id=run.id, recon_discrepancy_index=idx,
    )
    order = create_order(db, data, ctx["admin"])
    assert order.source == "recon_discrepancy"
    assert order.recon_code == "PROJECTION_CURRENT_MISMATCH"
    assert order.resolution_id is not None
    resolution = db.get(ReconDiscrepancyResolution, order.resolution_id)
    assert resolution.status == "pending"
    assert resolution.fingerprint and len(resolution.fingerprint) == 64

    # 企业不能根据对账差异转单（仅监管）
    with pytest.raises(RectificationError) as ei:
        create_order(db, data, ctx["ent1"])
    assert ei.value.http_status == 403

    # 同一差异再次转单：已有未终结工单 → 幂等返回原工单
    again = create_order(db, data, ctx["admin"])
    assert again.id == order.id

    # 企业整改提交后审核通过：处置单 resolved；篡改尚未修复 → 复验差异复发
    add_evidence(db, order.id, _evidence("盘点说明.pdf"), ctx["ent1"])
    submit_order(db, order.id, _submit_in("已完成账实核对（尚未修正余额）"), ctx["ent1"])
    order, result = approve_order(
        db, order.id, RectificationReviewIn(recalculate=False, followup_reconcile=True),
        ctx["verifier"],
    )
    resolution = db.get(ReconDiscrepancyResolution, order.resolution_id)
    assert resolution.status == "resolved"
    assert result["writeback"]["discrepancy_still_present"] is True
    assert "PROJECTION_CURRENT_MISMATCH" in result["writeback"]["recurred_codes"]
    assert result["followup_recon_status"] == "discrepancy"

    # 修复库外篡改（把余额改回去），再对账：差异消失
    account = db.query(AllowanceAccount).filter_by(company_id=c1.id, year=2026).one()
    account.current_balance = float(account.current_balance) - 50
    db.commit()
    run2 = run_reconciliation(db, scope="company", company_id=c1.id, year=2026)
    assert run2.status == "balanced"


def test_discrepancy_order_close_waives(ctx):
    db = ctx["db"]
    run = _make_tampered_run(db, ctx["c1"])
    issues = __import__("json").loads(run.discrepancies_json)
    idx = next(i for i, x in enumerate(issues) if x["code"] == "PROJECTION_CURRENT_MISMATCH")
    data = RectificationOrderIn(
        company_id=ctx["c1"].id, year=2026, title="差异核查免予整改",
        issue_type="reconciliation",
        recon_run_id=run.id, recon_discrepancy_index=idx,
    )
    order = create_order(db, data, ctx["admin"])
    order = close_order(db, order.id, "系统口径差异，经核查免予整改", "waived", ctx["admin"])
    resolution = db.get(ReconDiscrepancyResolution, order.resolution_id)
    assert resolution.status == "waived"
    assert order.status == CLOSED


def test_invalid_discrepancy_index(ctx):
    db = ctx["db"]
    run = run_reconciliation(db, scope="company", company_id=ctx["c1"].id, year=2026)
    with pytest.raises(RectificationError) as ei:
        create_order(db, RectificationOrderIn(
            company_id=ctx["c1"].id, year=2026, title="坏序号",
            recon_run_id=run.id, recon_discrepancy_index=99,
        ), ctx["admin"])
    assert ei.value.http_status == 404


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #

def test_full_audit_trail(ctx):
    db = ctx["db"]
    order = create_order(db, _order_in(ctx["c1"].id), ctx["admin"])
    add_evidence(db, order.id, _evidence(), ctx["ent1"])
    submit_order(db, order.id, _submit_in(), ctx["ent1"])
    reject_order(db, order.id, "材料不完整", ctx["verifier"])
    add_evidence(db, order.id, _evidence("补.pdf"), ctx["ent1"])
    submit_order(db, order.id, _submit_in("已补充"), ctx["ent1"])
    approve_order(db, order.id, RectificationReviewIn(followup_reconcile=False), ctx["verifier"])

    actions = [l.action for l in reversed(list_audit_logs(db, order_id=order.id))]
    assert actions == [
        "order.create", "evidence.upload", "order.submit",
        "order.reject", "evidence.upload", "order.submit", "order.approve",
    ]
    # 越权拒绝独立留痕
    logs_before = ctx["db"].query(CarbonRectificationAuditLog).count()
    write_audit(db, ctx["ent2"], "access.denied", order_id=order.id,
                detail="越权", result="denied", commit=True)
    denied = ctx["db"].query(CarbonRectificationAuditLog).filter_by(result="denied").one()
    assert denied.action == "access.denied"
    assert ctx["db"].query(CarbonRectificationAuditLog).count() == logs_before + 1
