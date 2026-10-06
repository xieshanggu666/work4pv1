"""碳排放整改工单 API：建单、举证提交、核查审核/驳回/关闭与结果回写、监管审计。

权限边界：
- 建单：监管（admin/verifier）可为任意企业登记，企业只能以本企业名义自查建单；
  对账差异转单仅监管；
- 证据/提交：企业只能操作本企业工单，监管可代登记证据但不代提交审核；
- 审核通过/驳回/关闭：仅 admin/verifier；
- 查询：企业仅见本企业工单，监管可见全部；审计与差异处置仅监管可见；
- 每一次越权拒绝写 carbon_rectification_audit_logs（独立提交留痕）。
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models import User
from app.schemas import (
    RectificationCloseIn,
    RectificationEvidenceIn,
    RectificationOrderIn,
    RectificationRejectIn,
    RectificationReviewIn,
    RectificationSubmitIn,
)
from app.services.rectification_service import (
    RectificationError,
    add_evidence,
    approve_order,
    close_order,
    create_order,
    list_audit_logs,
    list_orders,
    list_resolutions,
    serialize_audit,
    serialize_order,
    serialize_resolution,
    submit_order,
    write_audit,
    Operator,
)

router = APIRouter(prefix="/api/rectifications", tags=["rectifications"])

_REGULATORY = ("admin", "verifier")


def _operator(user: User, request: Request) -> Operator:
    return Operator(
        id=user.id,
        username=user.username,
        role=user.role,
        company_id=user.company_id,
        ip=(request.client.host if request.client else "") or "",
    )


def _audit_denied(db: Session, user: User, request: Request, action: str, detail: str) -> None:
    write_audit(db, _operator(user, request), action, detail=detail, result="denied", commit=True)


def _raise(err: RectificationError) -> None:
    raise HTTPException(status_code=err.http_status, detail=str(err))


@router.get("")
def list_rectifications(
    company_id: int | None = None,
    status: str | None = None,
    year: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """工单列表（企业强制本企业；监管可按企业/状态/年度过滤）。"""
    viewer = Operator(id=user.id, username=user.username, role=user.role, company_id=user.company_id)
    orders = list_orders(db, company_id=company_id, status=status, year=year, viewer=viewer)
    return [serialize_order(db, o) for o in orders]


@router.post("")
def create_rectification(
    data: RectificationOrderIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """创建整改工单（手工登记或对账差异转单，支持幂等键）。"""
    # 与账本接口一致：请求体或 Idempotency-Key 请求头均可携带幂等键
    if not data.idempotency_key:
        data.idempotency_key = request.headers.get("idempotency-key")
    op = _operator(user, request)
    try:
        order = create_order(db, data, op)
    except RectificationError as e:
        if e.http_status == 403:
            _audit_denied(db, user, request, "order.create", str(e))
        _raise(e)
    return serialize_order(db, order, with_evidences=True)


# 静态 GET 路由必须注册在 /{order_id} 之前，否则会被路径参数捕获
@router.get("/audit-logs")
def rectification_audit_logs(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """工单审计时间线（仅监管；企业越权读取 403 并留痕）。"""
    from app.services.rectification_service import _get_order

    if user.role not in _REGULATORY:
        _audit_denied(db, user, request, "access.denied",
                      f"越权读取工单 {order_id} 审计日志")
        raise HTTPException(status_code=403, detail="仅监管角色可查看整改审计")
    try:
        _get_order(db, order_id)
    except RectificationError as e:
        _raise(e)
    return [serialize_audit(log) for log in list_audit_logs(db, order_id=order_id)]


@router.get("/audit-logs/all")
def all_audit_logs(
    request: Request,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """整改监管审计全量（仅监管）。"""
    if user.role not in _REGULATORY:
        _audit_denied(db, user, request, "access.denied", "越权读取整改审计全量日志")
        raise HTTPException(status_code=403, detail="仅监管角色可查看整改审计")
    limit = max(1, min(int(limit), 500))
    return [serialize_audit(log) for log in list_audit_logs(db, limit=limit)]


@router.get("/discrepancy-resolutions/list")
def discrepancy_resolutions(
    company_id: int | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """对账差异处置清单（监管全部；企业仅本企业）。"""
    scope_company = company_id
    if user.role == "enterprise":
        scope_company = user.company_id
    return [
        serialize_resolution(r)
        for r in list_resolutions(db, company_id=scope_company, status=status)
    ]


@router.get("/{order_id}")
def rectification_detail(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """工单详情（含整改说明、证据清单与回写结果）。"""
    from app.services.rectification_service import _get_order

    try:
        order = _get_order(db, order_id)
    except RectificationError as e:
        _raise(e)
    if user.role == "enterprise" and user.company_id != order.company_id:
        _audit_denied(
            db, user, request, "access.denied",
            f"企业{user.company_id}越权查看工单 {order.order_no}（归属企业{order.company_id}）",
        )
        raise HTTPException(status_code=403, detail="无权查看该整改工单")
    return serialize_order(db, order, with_evidences=True)


@router.post("/{order_id}/evidences")
def upload_evidence(
    order_id: int,
    data: RectificationEvidenceIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """企业上传整改证据（方案/整改报告/佐证材料）。"""
    try:
        evidence = add_evidence(db, order_id, data, _operator(user, request))
    except RectificationError as e:
        if e.http_status == 403:
            _audit_denied(db, user, request, "evidence.upload", f"工单{order_id}：{e}")
        _raise(e)
    from app.services.rectification_service import serialize_evidence

    return serialize_evidence(evidence)


@router.post("/{order_id}/submit")
def submit_rectification(
    order_id: int,
    data: RectificationSubmitIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """企业提交整改（至少一份证据）：open → submitted 待核查。"""
    try:
        order = submit_order(db, order_id, data, _operator(user, request))
    except RectificationError as e:
        if e.http_status == 403:
            _audit_denied(db, user, request, "order.submit", f"工单{order_id}：{e}")
        _raise(e)
    return serialize_order(db, order, with_evidences=True)


@router.post("/{order_id}/approve")
def approve_rectification(
    order_id: int,
    data: RectificationReviewIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """核查员审核通过：结果回写对账差异处置与履约报告附注，可选重算+对账复验。"""
    if user.role not in _REGULATORY:
        _audit_denied(db, user, request, "order.approve", f"工单{order_id}：越权审核")
        raise HTTPException(status_code=403, detail="仅核查员/监管可审核整改工单")
    try:
        order, result = approve_order(db, order_id, data, _operator(user, request))
    except RectificationError as e:
        _raise(e)
    return {"order": serialize_order(db, order, with_evidences=True), **result}


@router.post("/{order_id}/reject")
def reject_rectification(
    order_id: int,
    data: RectificationRejectIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """核查员驳回（需原因）：submitted → open，企业整改后重新提交。"""
    if user.role not in _REGULATORY:
        _audit_denied(db, user, request, "order.reject", f"工单{order_id}：越权驳回")
        raise HTTPException(status_code=403, detail="仅核查员/监管可驳回整改工单")
    from app.services.rectification_service import reject_order as _reject

    try:
        order = _reject(db, order_id, data.reason, _operator(user, request))
    except RectificationError as e:
        _raise(e)
    return serialize_order(db, order, with_evidences=True)


@router.post("/{order_id}/close")
def close_rectification(
    order_id: int,
    data: RectificationCloseIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """监管关闭工单（问题不成立/免予整改，需原因），关联差异按 waived 处置。"""
    if user.role not in _REGULATORY:
        _audit_denied(db, user, request, "order.close", f"工单{order_id}：越权关闭")
        raise HTTPException(status_code=403, detail="仅核查员/监管可关闭整改工单")
    try:
        order = close_order(db, order_id, data.reason, data.resolve_discrepancy,
                            _operator(user, request))
    except RectificationError as e:
        _raise(e)
    return serialize_order(db, order, with_evidences=True)

