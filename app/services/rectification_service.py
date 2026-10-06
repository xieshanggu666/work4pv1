"""碳排放整改工单服务：建单 → 企业整改举证 → 核查审核/驳回/关闭 → 结果回写。

与既有模块的衔接：

- **对账差异**：工单可由某次对账运行（``ledger_reconciliations``，不可变）中的
  差异转单，差异身份用稳定指纹（:func:`discrepancy_fingerprint`，剔除易变数值）
  固化到 ``recon_discrepancy_resolutions``；审核通过置 resolved、关闭置 waived，
  后续对账运行再出现同一指纹的差异时，差异展示自动带“已处置/已豁免但复发”标记。
- **履约（MRV）报告**：审核通过时把整改结论作为**附注**只追加到该企业年度报告的
  ``rectification_notes_json``（不改写排放快照本身），批准/草稿/已冲正报告均可挂注；
  可选同事务重算排放量、刷新草稿（已批准报告受冻结快照保护，仅告警不重算）。
- **复验**：审核通过后可自动发起一次企业范围对账，验证差异是否真正消除。
- **审计**：建单/举证/提交/通过/驳回/关闭以及每一次越权拒绝都写
  ``carbon_rectification_audit_logs``；审计与业务变更同事务提交（同生共死），
  越权拒绝无业务事务时独立提交。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.ledger import LedgerReconciliation
from app.models.rectification import (
    CarbonRectificationAuditLog,
    CarbonRectificationEvidence,
    CarbonRectificationOrder,
    ReconDiscrepancyResolution,
)
from app.models.report import MrvReport
from app.services.calculation_service import annual_total, recalc_company_year
from app.services.mrv_service import _refresh_report_inner
from app.services.reconciliation_service import (
    discrepancy_fingerprint,
    run_reconciliation,
)

OPEN = "open"
SUBMITTED = "submitted"
APPROVED = "approved"
CLOSED = "closed"
TERMINAL_STATUSES = (APPROVED, CLOSED)

_REGULATORY_ROLES = ("admin", "verifier")


class RectificationError(ValueError):
    """整改工单业务规则不满足（状态非法、非参与方、证据缺失等）。"""

    def __init__(self, message: str, http_status: int = 400):
        super().__init__(message)
        self.http_status = http_status


@dataclass
class Operator:
    """审计操作人（API 层从登录态构造，service 层不感知 HTTP）。"""

    id: int | None
    username: str
    role: str
    company_id: int | None = None
    ip: str = ""


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #

def write_audit(
    db: Session,
    operator: Operator | None,
    action: str,
    *,
    target_type: str = "",
    target_id: int | None = None,
    order_id: int | None = None,
    detail: str = "",
    result: str = "success",
    commit: bool = False,
) -> CarbonRectificationAuditLog:
    """写一条整改监管审计；业务操作默认同事务提交，越权拒绝可独立 commit。"""
    log = CarbonRectificationAuditLog(
        operator_id=operator.id if operator else None,
        operator_name=operator.username if operator else "anonymous",
        operator_role=operator.role if operator else "",
        action=action,
        target_type=target_type,
        target_id=target_id,
        order_id=order_id,
        detail=(detail or "")[:500],
        result=result,
        ip=operator.ip if operator else "",
    )
    db.add(log)
    db.flush()
    if commit:
        db.commit()
        db.refresh(log)
    return log


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def _gen_order_no() -> str:
    return f"ZG{datetime.utcnow():%Y%m%d%H%M%S}{uuid.uuid4().hex[:6].upper()}"


def _get_order(db: Session, order_id: int) -> CarbonRectificationOrder:
    order = db.get(CarbonRectificationOrder, order_id)
    if order is None:
        raise RectificationError("整改工单不存在", http_status=404)
    return order


def _get_company(db: Session, company_id: int) -> Company:
    company = db.get(Company, company_id)
    if company is None:
        raise RectificationError("企业不存在", http_status=404)
    return company


def _load_json(value: str, default):
    try:
        return json.loads(value or "")
    except (ValueError, TypeError):
        return default


def _resolution_scope_company(refs: dict, run: LedgerReconciliation | None) -> int | None:
    if refs.get("company_id") is not None:
        return int(refs["company_id"])
    if run and run.scope == "company":
        return run.company_id
    return None


def _resolution_scope_year(refs: dict, run: LedgerReconciliation | None) -> int | None:
    if refs.get("year") is not None:
        return int(refs["year"])
    if run and run.scope == "year":
        return run.year
    return None


def _get_run_issue(db: Session, run_id: int, index: int) -> tuple[LedgerReconciliation, dict]:
    """从对账运行的差异列表中按序号定位一条差异。"""
    run = db.get(LedgerReconciliation, run_id)
    if run is None:
        raise RectificationError("对账运行不存在", http_status=404)
    issues = _load_json(run.discrepancies_json, [])
    if not isinstance(index, int) or index < 0 or index >= len(issues):
        raise RectificationError(
            f"对账差异序号无效（运行 {run.recon_no} 共 {len(issues)} 条差异）",
            http_status=404,
        )
    return run, issues[index]


# --------------------------------------------------------------------------- #
# 建单（含对账差异转单）
# --------------------------------------------------------------------------- #

def _find_idempotent_order(db: Session, key: str | None) -> CarbonRectificationOrder | None:
    if not key:
        return None
    return (
        db.query(CarbonRectificationOrder)
        .filter(CarbonRectificationOrder.idempotency_key == key)
        .first()
    )


def create_order(db: Session, data, operator: Operator) -> CarbonRectificationOrder:
    """建整改工单。

    - 监管（admin/verifier）可为任意企业登记；企业只能以本企业名义自查建单；
    - 由对账差异转单（``recon_run_id`` + ``recon_discrepancy_index``）时固化
      差异指纹与 pending 处置单，同一指纹已有未终结工单时幂等返回原工单；
    - 建单与审计、差异处置登记同事务提交。
    """
    if operator.role not in _REGULATORY_ROLES and operator.role != "enterprise":
        raise RectificationError("无权创建整改工单", http_status=403)
    if operator.role == "enterprise" and operator.company_id != data.company_id:
        raise RectificationError("企业只能为本企业登记整改工单", http_status=403)
    _get_company(db, data.company_id)

    existing = _find_idempotent_order(db, data.idempotency_key)
    if existing is not None:
        return existing

    run: LedgerReconciliation | None = None
    issue: dict | None = None
    resolution: ReconDiscrepancyResolution | None = None
    if data.recon_run_id is not None:
        if data.recon_discrepancy_index is None:
            raise RectificationError("对账差异转单需提供差异序号")
        if operator.role not in _REGULATORY_ROLES:
            raise RectificationError("仅监管角色可根据对账差异下发整改工单", http_status=403)
        run, issue = _get_run_issue(db, data.recon_run_id, data.recon_discrepancy_index)
        refs = issue.get("refs") or {}
        company_scope = run.company_id if run.scope == "company" else None
        year_scope = run.year if run.scope == "year" else None
        fingerprint = discrepancy_fingerprint(
            issue, company_scope=company_scope, year_scope=year_scope
        )
        # 同一差异（指纹）已有未终结工单：幂等收敛，避免重复下发
        resolution = (
            db.query(ReconDiscrepancyResolution)
            .filter(ReconDiscrepancyResolution.fingerprint == fingerprint)
            .first()
        )
        if resolution is not None and resolution.order_id:
            prior = db.get(CarbonRectificationOrder, resolution.order_id)
            if prior is not None and prior.status not in TERMINAL_STATUSES:
                return prior
        if resolution is None:
            resolution = ReconDiscrepancyResolution(
                recon_run_id=run.id,
                company_id=_resolution_scope_company(refs, run),
                year=_resolution_scope_year(refs, run),
                discrepancy_code=issue.get("code", ""),
                refs_json=json.dumps(refs, ensure_ascii=False),
                fingerprint=fingerprint,
                status="pending",
            )
            db.add(resolution)
            db.flush()

    title = data.title.strip()
    order = CarbonRectificationOrder(
        order_no=_gen_order_no(),
        company_id=data.company_id,
        year=data.year,
        source="recon_discrepancy" if run is not None else "manual",
        title=title,
        issue_type=data.issue_type,
        description=(data.description or "").strip(),
        requirement=(data.requirement or "").strip(),
        due_date=(data.due_date or "").strip(),
        status=OPEN,
        created_by=operator.id,
        creator_role=operator.role,
        recon_run_id=run.id if run else None,
        recon_code=(issue or {}).get("code", "") if run else "",
        recon_refs_json=json.dumps((issue or {}).get("refs") or {}, ensure_ascii=False) if run else "",
        recon_message=((issue or {}).get("message", "")) if run else "",
        resolution_id=resolution.id if resolution else None,
        idempotency_key=data.idempotency_key,
    )
    db.add(order)
    db.flush()
    if resolution is not None:
        resolution.order_id = order.id

    write_audit(
        db, operator, "order.create", target_type="company", target_id=order.company_id,
        order_id=order.id,
        detail=f"创建整改工单 {order.order_no}：{title}"
               + (f"（对账差异 {order.recon_code} 转单）" if run else ""),
    )
    db.commit()
    db.refresh(order)
    return order


# --------------------------------------------------------------------------- #
# 证据与整改提交
# --------------------------------------------------------------------------- #

def add_evidence(db: Session, order_id: int, data, operator: Operator) -> CarbonRectificationEvidence:
    """企业为本企业 open/submitted 工单上传整改证据（方案/报告/佐证材料）。"""
    if operator.role not in ("enterprise", *_REGULATORY_ROLES):
        raise RectificationError("无权上传整改证据", http_status=403)
    order = _get_order(db, order_id)
    if operator.role == "enterprise" and operator.company_id != order.company_id:
        raise RectificationError("只能为本企业工单上传证据", http_status=403)
    if order.status in TERMINAL_STATUSES:
        raise RectificationError("工单已终结，不能再补充证据")

    evidence = CarbonRectificationEvidence(
        order_id=order.id,
        company_id=order.company_id,
        evidence_type=data.evidence_type,
        file_name=data.file_name.strip(),
        file_url=data.file_url.strip(),
        file_hash=data.file_hash.strip(),
        file_size=int(data.file_size or 0),
        description=(data.description or "").strip(),
        uploaded_by=operator.id,
    )
    db.add(evidence)
    db.flush()
    write_audit(
        db, operator, "evidence.upload", target_type="evidence", target_id=evidence.id,
        order_id=order.id, detail=f"工单 {order.order_no} 上传证据：{evidence.file_name}",
    )
    db.commit()
    db.refresh(evidence)
    return evidence


def submit_order(db: Session, order_id: int, data, operator: Operator) -> CarbonRectificationOrder:
    """企业提交整改：保存整改说明，可选随附证据，状态 open → submitted。

    open 与被驳回（驳回后回到 open）的工单均可提交；至少需要一份已登记证据。
    """
    order = _get_order(db, order_id)
    if operator.role == "enterprise":
        if operator.company_id != order.company_id:
            raise RectificationError("只能提交本企业的整改工单", http_status=403)
    elif operator.role not in _REGULATORY_ROLES:
        raise RectificationError("无权提交整改工单", http_status=403)
    if order.status != OPEN:
        raise RectificationError("仅待整改（含被驳回）的工单可提交")

    if data.evidences:
        for ev in data.evidences:
            db.add(CarbonRectificationEvidence(
                order_id=order.id,
                company_id=order.company_id,
                evidence_type=ev.evidence_type,
                file_name=ev.file_name.strip(),
                file_url=ev.file_url.strip(),
                file_hash=ev.file_hash.strip(),
                file_size=int(ev.file_size or 0),
                description=(ev.description or "").strip(),
                uploaded_by=operator.id,
            ))
        db.flush()

    evidence_count = (
        db.query(CarbonRectificationEvidence)
        .filter(CarbonRectificationEvidence.order_id == order.id)
        .count()
    )
    if evidence_count <= 0:
        raise RectificationError("请至少上传一份整改证据材料后再提交")

    order.submission_json = json.dumps({
        "summary": data.summary.strip(),
        "measures": (data.measures or "").strip(),
        "impact": (data.impact or "").strip(),
    }, ensure_ascii=False)
    order.submitted_by = operator.id
    order.submitted_at = datetime.utcnow()
    order.status = SUBMITTED
    db.flush()
    write_audit(
        db, operator, "order.submit", target_type="order", target_id=order.id,
        order_id=order.id,
        detail=f"工单 {order.order_no} 提交整改（证据 {evidence_count} 份），待核查员审核",
    )
    db.commit()
    db.refresh(order)
    return order


# --------------------------------------------------------------------------- #
# 核查审核：通过 / 驳回 / 关闭
# --------------------------------------------------------------------------- #

def _annotate_report(db: Session, order: CarbonRectificationOrder, comment: str,
                     operator: Operator, payload: dict) -> None:
    """把整改结论作为附注只追加到该企业年度 MRV 报告（不改写排放快照）。"""
    if not order.year:
        return
    report = (
        db.query(MrvReport)
        .filter(MrvReport.company_id == order.company_id, MrvReport.year == order.year)
        .first()
    )
    if report is None:
        return
    notes = _load_json(report.rectification_notes_json, [])
    if not isinstance(notes, list):
        notes = []
    note = {
        "order_id": order.id,
        "order_no": order.order_no,
        "title": order.title,
        "issue_type": order.issue_type,
        "review_comment": (comment or "").strip(),
        "submission": _load_json(order.submission_json, {}),
        "reviewed_by": operator.id,
        "reviewed_at": datetime.utcnow().isoformat(),
    }
    notes.append(note)
    report.rectification_notes_json = json.dumps(notes, ensure_ascii=False)
    db.flush()
    payload["report_annotated"] = True
    payload["report_id"] = report.id
    payload["report_status"] = report.status


def _finalize_resolution(db: Session, order: CarbonRectificationOrder, status: str,
                         comment: str, operator: Operator) -> dict | None:
    """把审核结论回写对账差异处置单（resolved/waived）。"""
    if not order.resolution_id:
        return None
    resolution = db.get(ReconDiscrepancyResolution, order.resolution_id)
    if resolution is None:
        return None
    resolution.status = status
    resolution.comment = (comment or "").strip()
    resolution.resolved_by = operator.id
    resolution.resolved_at = datetime.utcnow()
    db.flush()
    return {
        "resolution_id": resolution.id,
        "fingerprint": resolution.fingerprint,
        "discrepancy_code": resolution.discrepancy_code,
        "status": resolution.status,
    }


def approve_order(db: Session, order_id: int, data, operator: Operator) -> tuple[CarbonRectificationOrder, dict]:
    """核查员审核通过：回写对账差异处置与履约报告附注，可选重算与对账复验。

    回写、（可选）重算与审计在同一事务提交；复验对账在事务提交后发起（对账是
    独立运行记录），其运行号回填工单。
    """
    if operator.role not in _REGULATORY_ROLES:
        raise RectificationError("仅核查员/监管可审核整改工单", http_status=403)
    order = _get_order(db, order_id)
    if order.status != SUBMITTED:
        raise RectificationError("仅企业已提交整改的工单可审核通过")

    comment = (data.comment or "").strip()
    payload: dict = {
        "review_comment": comment,
        "recalculated": False,
        "report_annotated": False,
    }

    order.status = APPROVED
    order.reviewed_by = operator.id
    order.reviewed_at = datetime.utcnow()
    order.review_comment = comment
    order.reject_reason = ""

    resolution_info = _finalize_resolution(
        db, order, data.resolve_discrepancy, comment or "整改完成，审核通过", operator
    )
    if resolution_info:
        payload["resolution"] = resolution_info

    _annotate_report(db, order, comment, operator, payload)

    # 可选同事务重算排放量并刷新报告草稿；已批准报告受冻结快照保护，只告警
    recalc_warning = ""
    if data.recalculate and order.year:
        approved_report = (
            db.query(MrvReport.id)
            .filter(
                MrvReport.company_id == order.company_id,
                MrvReport.year == order.year,
                MrvReport.status == "approved",
            ).first()
        )
        if approved_report:
            recalc_warning = (
                f"{order.year}年度报告已批准并冻结配额，未自动重算；"
                "如需调整排放量请先冲正批准报告"
            )
            payload["recalc_skipped_reason"] = "report_approved"
        else:
            emission_before = annual_total(db, order.company_id, order.year)
            count = recalc_company_year(db, order.company_id, order.year, commit=False)
            emission_after = annual_total(db, order.company_id, order.year)
            action, warning = _refresh_report_inner(db, order.company_id, order.year)
            payload.update({
                "recalculated": True,
                "result_count": count,
                "report_action": action,
                "emission_before": round(float(emission_before), 4),
                "emission_after": round(float(emission_after), 4),
            })
            if warning:
                recalc_warning = warning

    db.flush()
    write_audit(
        db, operator, "order.approve", target_type="order", target_id=order.id,
        order_id=order.id,
        detail=f"工单 {order.order_no} 审核通过"
               + ("，对账差异处置：" + data.resolve_discrepancy if resolution_info else ""),
    )
    order.corrective_action_json = json.dumps(payload, ensure_ascii=False)
    db.commit()
    db.refresh(order)

    # 审核提交后发起企业范围复验对账（独立运行记录），验证差异是否真正消除
    followup_status = None
    if data.followup_reconcile:
        run = run_reconciliation(
            db,
            scope="company",
            company_id=order.company_id,
            year=order.year,
            triggered_by=operator.id,
        )
        order.followup_recon_run_id = run.id
        still = []
        if order.resolution_id:
            resolution = db.get(ReconDiscrepancyResolution, order.resolution_id)
            if resolution:
                issues = _load_json(run.discrepancies_json, [])
                company_scope = run.company_id
                year_scope = run.year
                for issue in issues:
                    if discrepancy_fingerprint(
                        issue, company_scope=company_scope, year_scope=year_scope
                    ) == resolution.fingerprint:
                        still.append(issue.get("code"))
        payload["followup_recon_run_id"] = run.id
        payload["followup_recon_status"] = run.status
        payload["discrepancy_still_present"] = bool(still)
        if still:
            payload["recurred_codes"] = still
        followup_status = run.status
        order.corrective_action_json = json.dumps(payload, ensure_ascii=False)
        db.commit()
        db.refresh(order)

    result = {"writeback": payload}
    if recalc_warning:
        result["warning"] = recalc_warning
    if followup_status:
        result["followup_recon_status"] = followup_status
    return order, result


def reject_order(db: Session, order_id: int, reason: str, operator: Operator) -> CarbonRectificationOrder:
    """核查员驳回：填写原因，工单回到 open 由企业重新整改（可反复驳回）。"""
    if operator.role not in _REGULATORY_ROLES:
        raise RectificationError("仅核查员/监管可驳回整改工单", http_status=403)
    order = _get_order(db, order_id)
    if order.status != SUBMITTED:
        raise RectificationError("仅待审核的工单可驳回")
    reason = (reason or "").strip()
    if len(reason) < 2:
        raise RectificationError("请填写驳回原因（至少 2 个字符）")

    order.status = OPEN
    order.reject_reason = reason
    order.rejected_at = datetime.utcnow()
    order.rejection_count = int(order.rejection_count or 0) + 1
    db.flush()
    write_audit(
        db, operator, "order.reject", target_type="order", target_id=order.id,
        order_id=order.id,
        detail=f"工单 {order.order_no} 第 {order.rejection_count} 次驳回：{reason}",
    )
    db.commit()
    db.refresh(order)
    return order


def close_order(db: Session, order_id: int, reason: str, resolve_mode: str,
                operator: Operator) -> CarbonRectificationOrder:
    """监管关闭工单（问题不成立/免予整改）；关联对账差异按 waived（或 resolved）处置。"""
    if operator.role not in _REGULATORY_ROLES:
        raise RectificationError("仅核查员/监管可关闭整改工单", http_status=403)
    order = _get_order(db, order_id)
    if order.status in TERMINAL_STATUSES:
        raise RectificationError("工单已终结")
    reason = (reason or "").strip()
    if len(reason) < 2:
        raise RectificationError("请填写关闭原因（至少 2 个字符）")

    order.status = CLOSED
    order.closed_by = operator.id
    order.closed_at = datetime.utcnow()
    order.close_reason = reason

    payload: dict = {"close_reason": reason}
    resolution_info = _finalize_resolution(db, order, resolve_mode, reason, operator)
    if resolution_info:
        payload["resolution"] = resolution_info
    order.corrective_action_json = json.dumps(payload, ensure_ascii=False)
    db.flush()
    write_audit(
        db, operator, "order.close", target_type="order", target_id=order.id,
        order_id=order.id,
        detail=f"工单 {order.order_no} 关闭：{reason}"
               + ("（对账差异处置：" + resolve_mode + "）" if resolution_info else ""),
    )
    db.commit()
    db.refresh(order)
    return order


# --------------------------------------------------------------------------- #
# 查询与序列化
# --------------------------------------------------------------------------- #

def list_orders(db: Session, *, company_id: int | None, status: str | None,
                year: int | None, viewer: Operator) -> list[CarbonRectificationOrder]:
    q = db.query(CarbonRectificationOrder)
    if viewer.role == "enterprise":
        q = q.filter(CarbonRectificationOrder.company_id == viewer.company_id)
    elif company_id is not None:
        q = q.filter(CarbonRectificationOrder.company_id == company_id)
    if status:
        q = q.filter(CarbonRectificationOrder.status == status)
    if year is not None:
        q = q.filter(CarbonRectificationOrder.year == year)
    return q.order_by(CarbonRectificationOrder.id.desc()).all()


def list_evidences(db: Session, order_id: int) -> list[CarbonRectificationEvidence]:
    return (
        db.query(CarbonRectificationEvidence)
        .filter(CarbonRectificationEvidence.order_id == order_id)
        .order_by(CarbonRectificationEvidence.id.asc())
        .all()
    )


def list_audit_logs(db: Session, *, order_id: int | None = None,
                    limit: int = 200) -> list[CarbonRectificationAuditLog]:
    q = db.query(CarbonRectificationAuditLog)
    if order_id is not None:
        q = q.filter(CarbonRectificationAuditLog.order_id == order_id)
    return q.order_by(CarbonRectificationAuditLog.id.desc()).limit(limit).all()


def list_resolutions(db: Session, *, company_id: int | None = None,
                     status: str | None = None) -> list[ReconDiscrepancyResolution]:
    q = db.query(ReconDiscrepancyResolution)
    if company_id is not None:
        q = q.filter(ReconDiscrepancyResolution.company_id == company_id)
    if status:
        q = q.filter(ReconDiscrepancyResolution.status == status)
    return q.order_by(ReconDiscrepancyResolution.id.desc()).all()


def serialize_evidence(ev: CarbonRectificationEvidence) -> dict:
    return {
        "id": ev.id,
        "order_id": ev.order_id,
        "company_id": ev.company_id,
        "evidence_type": ev.evidence_type,
        "file_name": ev.file_name,
        "file_url": ev.file_url,
        "file_hash": ev.file_hash,
        "file_size": ev.file_size,
        "description": ev.description,
        "uploaded_by": ev.uploaded_by,
        "created_at": ev.created_at,
    }


def serialize_order(db: Session, order: CarbonRectificationOrder,
                    *, with_evidences: bool = False) -> dict:
    company = db.get(Company, order.company_id)
    data = {
        "id": order.id,
        "order_no": order.order_no,
        "company_id": order.company_id,
        "company_name": company.name if company else str(order.company_id),
        "year": order.year,
        "source": order.source,
        "title": order.title,
        "issue_type": order.issue_type,
        "description": order.description,
        "requirement": order.requirement,
        "due_date": order.due_date,
        "status": order.status,
        "created_by": order.created_by,
        "creator_role": order.creator_role,
        "submission": _load_json(order.submission_json, {}),
        "submitted_by": order.submitted_by,
        "submitted_at": order.submitted_at,
        "reviewed_by": order.reviewed_by,
        "reviewed_at": order.reviewed_at,
        "review_comment": order.review_comment,
        "reject_reason": order.reject_reason,
        "rejected_at": order.rejected_at,
        "rejection_count": order.rejection_count,
        "closed_by": order.closed_by,
        "closed_at": order.closed_at,
        "close_reason": order.close_reason,
        "corrective_action": _load_json(order.corrective_action_json, {}),
        "recon_run_id": order.recon_run_id,
        "recon_code": order.recon_code,
        "recon_refs": _load_json(order.recon_refs_json, {}),
        "recon_message": order.recon_message,
        "resolution_id": order.resolution_id,
        "followup_recon_run_id": order.followup_recon_run_id,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
    }
    if with_evidences:
        data["evidences"] = [serialize_evidence(ev) for ev in list_evidences(db, order.id)]
        data["evidence_count"] = len(data["evidences"])
    else:
        data["evidence_count"] = (
            db.query(CarbonRectificationEvidence)
            .filter(CarbonRectificationEvidence.order_id == order.id)
            .count()
        )
    return data


def serialize_audit(log: CarbonRectificationAuditLog) -> dict:
    return {
        "id": log.id,
        "operator_id": log.operator_id,
        "operator_name": log.operator_name,
        "operator_role": log.operator_role,
        "action": log.action,
        "target_type": log.target_type,
        "target_id": log.target_id,
        "order_id": log.order_id,
        "detail": log.detail,
        "result": log.result,
        "ip": log.ip,
        "created_at": log.created_at,
    }


def serialize_resolution(r: ReconDiscrepancyResolution) -> dict:
    return {
        "id": r.id,
        "recon_run_id": r.recon_run_id,
        "company_id": r.company_id,
        "year": r.year,
        "discrepancy_code": r.discrepancy_code,
        "refs": _load_json(r.refs_json, {}),
        "fingerprint": r.fingerprint,
        "status": r.status,
        "order_id": r.order_id,
        "comment": r.comment,
        "resolved_by": r.resolved_by,
        "resolved_at": r.resolved_at,
        "created_at": r.created_at,
    }
