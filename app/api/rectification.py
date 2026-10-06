"""碳排放整改工单 API：监管/核查开单，企业整改举证，核查审核、驳回与关闭。

权限边界
========
- 开单 / 审核（通过/驳回）/ 关闭 / 重跑对账回写：admin、verifier（监管侧）；
- 提交整改：仅 enterprise，且只能提交本企业的工单；
- 列表/详情：企业只见本企业工单，监管侧可见全部；
- 审计日志：仅监管侧可读，企业越权读取 403 并留痕；
- 每一次越权拒绝均写 rectification_audit_logs（result=denied）。
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models import Company, RectificationOrder, User
from app.models.rectification import RectificationEvidence
from app.schemas import (
    RectificationCloseIn,
    RectificationOrderIn,
    RectificationReviewIn,
    RectificationSubmitIn,
)
from app.services.rectification_service import (
    RectificationError,
    Operator,
    close_order,
    create_order,
    list_audit_logs,
    list_evidences,
    list_orders,
    rerun_writeback,
    review_order,
    submit_rectification,
    write_audit,
)

router = APIRouter(prefix="/api/rectifications", tags=["rectifications"])

_REGULATORY = ("admin", "verifier")
_SOURCE_LABEL = {
    "reconciliation": "对账差异",
    "report": "报告问题",
    "activity": "活动数据",
    "manual": "监管开单",
}


def _operator(user: User, request: Request) -> Operator:
    return Operator(
        id=user.id,
        username=user.username,
        role=user.role,
        ip=(request.client.host if request.client else "") or "",
    )


def _audit_denied(db: Session, user: User, request: Request, action: str, detail: str) -> None:
    """越权拒绝即时落审计（无业务事务，独立提交）。"""
    write_audit(
        db,
        _operator(user, request),
        action,
        detail=detail,
        result="denied",
        commit=True,
    )


def _serialize_order(db: Session, order: RectificationOrder) -> dict:
    company = db.get(Company, order.company_id)
    return {
        "id": order.id,
        "order_no": order.order_no,
        "company_id": order.company_id,
        "company_name": company.name if company else str(order.company_id),
        "year": order.year,
        "status": order.status,
        "title": order.title,
        "description": order.description,
        "source_type": order.source_type,
        "source_label": _SOURCE_LABEL.get(order.source_type, order.source_type),
        "reconciliation_id": order.reconciliation_id,
        "report_id": order.report_id,
        "activity_id": order.activity_id,
        "discrepancy_codes": json.loads(order.discrepancy_codes or "[]"),
        "due_date": order.due_date,
        "issued_by": order.issued_by,
        "issued_at": order.issued_at,
        "rectification_measure": order.rectification_measure,
        "emission_adjustment": (
            float(order.emission_adjustment) if order.emission_adjustment is not None else None
        ),
        "confirmed_emission_adjustment": (
            float(order.confirmed_emission_adjustment)
            if order.confirmed_emission_adjustment is not None else None
        ),
        "submitted_by": order.submitted_by,
        "submitted_at": order.submitted_at,
        "submit_count": order.submit_count,
        "review_comment": order.review_comment,
        "reviewed_by": order.reviewed_by,
        "reviewed_at": order.reviewed_at,
        "close_reason": order.close_reason,
        "closed_by": order.closed_by,
        "closed_at": order.closed_at,
        "writeback_reconciliation_id": order.writeback_reconciliation_id,
        "writeback_status": order.writeback_status,
        "writeback_discrepancy_count": order.writeback_discrepancy_count,
        "writeback_at": order.writeback_at,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
    }


def _serialize_evidence(ev: RectificationEvidence) -> dict:
    return {
        "id": ev.id,
        "order_id": ev.order_id,
        "round": ev.round,
        "evidence_type": ev.evidence_type,
        "name": ev.name,
        "file_url": ev.file_url,
        "remark": ev.remark,
        "uploaded_by": ev.uploaded_by,
        "created_at": ev.created_at,
    }


# --------------------------------------------------------------------------- #
# 监管/核查：开单
# --------------------------------------------------------------------------- #

@router.post("")
def create(
    request: Request,
    data: RectificationOrderIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role not in _REGULATORY:
        _audit_denied(
            db, user, request, "order.create",
            f"{user.role} 角色试图开具碳排放整改工单",
        )
        raise HTTPException(status_code=403, detail="仅监管/核查角色可开具整改工单")
    idem = data.idempotency_key or request.headers.get("idempotency-key")
    try:
        order = create_order(
            db,
            company_id=data.company_id,
            year=data.year,
            title=data.title,
            description=data.description,
            operator=_operator(user, request),
            source_type=data.source_type,
            reconciliation_id=data.reconciliation_id,
            report_id=data.report_id,
            activity_id=data.activity_id,
            discrepancy_codes=data.discrepancy_codes,
            due_date=data.due_date,
            idempotency_key=idem,
        )
    except RectificationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_order(db, order)


# --------------------------------------------------------------------------- #
# 列表 / 审计
# --------------------------------------------------------------------------- #

@router.get("")
def list_orders_api(
    year: int | None = None,
    status: str | None = None,
    source_type: str | None = None,
    company_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """工单列表：企业强制收敛到本企业；监管可按企业过滤。"""
    scoped_company = user.company_id if user.role == "enterprise" else company_id
    items = list_orders(
        db, company_id=scoped_company, year=year, status=status, source_type=source_type
    )
    return [_serialize_order(db, x) for x in items]


@router.get("/audit-logs")
def audit_logs(
    request: Request,
    order_id: int | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role not in _REGULATORY:
        _audit_denied(
            db, user, request, "audit.read",
            f"企业 {user.company_id} 试图读取整改监管审计日志",
        )
        raise HTTPException(status_code=403, detail="仅监管角色可查看整改审计日志")
    limit = max(1, min(limit, 500))
    logs = list_audit_logs(db, order_id=order_id, limit=limit)
    return [
        {
            "id": x.id,
            "operator_id": x.operator_id,
            "operator_name": x.operator_name,
            "operator_role": x.operator_role,
            "action": x.action,
            "target_type": x.target_type,
            "target_id": x.target_id,
            "order_id": x.order_id,
            "detail": x.detail,
            "result": x.result,
            "ip": x.ip,
            "created_at": x.created_at,
        }
        for x in logs
    ]


@router.get("/{order_id}")
def detail(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    order = db.get(RectificationOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="整改工单不存在")
    if user.role == "enterprise" and user.company_id != order.company_id:
        raise HTTPException(status_code=403, detail="无权查看该企业的整改工单")
    body = _serialize_order(db, order)
    body["evidences"] = [_serialize_evidence(ev) for ev in list_evidences(db, order.id)]
    return body


# --------------------------------------------------------------------------- #
# 企业：提交整改
# --------------------------------------------------------------------------- #

@router.post("/{order_id}/submit")
def submit(
    order_id: int,
    request: Request,
    data: RectificationSubmitIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    order = db.get(RectificationOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="整改工单不存在")
    if user.role != "enterprise":
        _audit_denied(
            db, user, request, "order.submit",
            f"{user.role} 角色试图代企业提交整改工单 {order.order_no}",
        )
        raise HTTPException(status_code=403, detail="仅控排企业可提交整改")
    if user.company_id != order.company_id:
        _audit_denied(
            db, user, request, "order.submit",
            f"企业 {user.company_id} 试图提交企业 {order.company_id} 的工单 {order.order_no}",
        )
        raise HTTPException(status_code=403, detail="只能提交本企业的整改工单")
    try:
        order = submit_rectification(
            db,
            order_id,
            company_id=user.company_id,
            operator=_operator(user, request),
            rectification_measure=data.rectification_measure,
            emission_adjustment=data.emission_adjustment,
            evidences=[ev.model_dump() for ev in data.evidences],
        )
    except RectificationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_order(db, order)


# --------------------------------------------------------------------------- #
# 核查员：审核（通过 / 驳回）
# --------------------------------------------------------------------------- #

@router.post("/{order_id}/review")
def review(
    order_id: int,
    request: Request,
    data: RectificationReviewIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    order = db.get(RectificationOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="整改工单不存在")
    if user.role not in _REGULATORY:
        _audit_denied(
            db, user, request, "review.denied",
            f"{user.role} 角色试图审核整改工单 {order.order_no}",
        )
        raise HTTPException(status_code=403, detail="仅核查员/监管可审核整改工单")
    try:
        order = review_order(
            db,
            order_id,
            operator=_operator(user, request),
            approved=data.approved,
            comment=data.comment,
            confirmed_emission_adjustment=data.confirmed_emission_adjustment,
        )
    except RectificationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_order(db, order)


@router.post("/{order_id}/rerun-writeback")
def rerun_writeback_api(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """审核通过后重新运行企业年度对账并回写工单（对账失败重试/监管复核）。"""
    order = db.get(RectificationOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="整改工单不存在")
    if user.role not in _REGULATORY:
        _audit_denied(
            db, user, request, "review.writeback.denied",
            f"{user.role} 角色试图触发整改工单 {order.order_no} 的对账回写",
        )
        raise HTTPException(status_code=403, detail="仅核查员/监管可触发对账回写")
    try:
        order = rerun_writeback(db, order_id, operator=_operator(user, request))
    except RectificationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_order(db, order)


# --------------------------------------------------------------------------- #
# 监管：关闭
# --------------------------------------------------------------------------- #

@router.post("/{order_id}/close")
def close(
    order_id: int,
    request: Request,
    data: RectificationCloseIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    order = db.get(RectificationOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="整改工单不存在")
    if user.role not in _REGULATORY:
        _audit_denied(
            db, user, request, "order.close.denied",
            f"{user.role} 角色试图关闭整改工单 {order.order_no}",
        )
        raise HTTPException(status_code=403, detail="仅监管/核查角色可关闭整改工单")
    try:
        order = close_order(
            db, order_id, operator=_operator(user, request), reason=data.reason
        )
    except RectificationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_order(db, order)
