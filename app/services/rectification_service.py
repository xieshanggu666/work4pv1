"""碳排放整改工单服务：开单 → 企业整改举证 → 核查审核 → 结果回写对账与履约报告。

设计要点
========

- **状态机收敛**：提交要求工单处于 open/rejected，审核要求处于 submitted，
  关闭要求非终态；全部流转先在写事务内重读并校验状态，重复/双击自然拒绝。
- **证据链完整**：企业每被驳回一次后重新提交，证据轮次 round +1，历史证据
  与审核记录均不删除，形成「问题 → 多轮整改 → 认定」的完整轨迹。
- **结果回写（审核通过，同一业务事务）**：
  1. 整改结论（措施、认定排放调整量、审核意见）追加写入关联 MRV 报告的
     ``report_json.rectifications`` 列表（append-only，不改排放快照本身）；
  2. 随后自动运行一次企业+年度范围对账，对账结论（balanced/discrepancy、
     差异数）挂接工单 ``writeback_*`` 字段——对账差异经整改后再核对，
     形成「对账发现差异 → 整改 → 复核」闭环；
  3. 对账本身只读业务数据（其运行记录独立提交），对账失败不回滚审核结论，
     工单标记 writeback_status=failed 供监管事后重跑，业务一致性不受影响。
- **监管审计**：开单/提交/通过/驳回/关闭与越权拒绝全部写
  ``rectification_audit_logs``；业务操作审计与业务变更同事务提交（同生共死），
  越权拒绝由 API 层独立提交落库。

注意：整改不直接做配额划转，也不直接改写已批准报告的排放快照（那会破坏
报告批准↔履约冻结↔清缴的账本闭环）。整改认定排放需要调整时，仍须走
「活动数据核验 → 重算 → 报告冲正重新批准」的既有链路；工单只登记认定结论
并驱动对账复核，避免在账本上开旁路。
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.emission import ActivityData
from app.models.ledger import LedgerReconciliation
from app.models.rectification import (
    APPROVED,
    CLOSED,
    OPEN,
    REJECTED,
    SOURCE_ACTIVITY,
    SOURCE_MANUAL,
    SOURCE_RECONCILIATION,
    SOURCE_REPORT,
    SOURCE_TYPES,
    SUBMITTED,
    TERMINAL_STATUSES,
    RectificationAuditLog,
    RectificationEvidence,
    RectificationOrder,
)
from app.models.report import MrvReport
from app.services.reconciliation_service import run_reconciliation

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class RectificationError(ValueError):
    """整改工单业务规则不满足（状态非法、来源单据缺失、越权等）。"""


@dataclass
class Operator:
    """审计操作人（API 层从登录态构造，service 层不感知 HTTP）。"""

    id: int | None
    username: str
    role: str
    ip: str = ""


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #

def write_audit(
    db: Session,
    operator: Operator | None,
    action: str,
    *,
    target_type: str = "order",
    target_id: int | None = None,
    order_id: int | None = None,
    detail: str = "",
    result: str = "success",
    commit: bool = False,
) -> RectificationAuditLog:
    """写一条整改监管审计日志。

    业务操作默认 commit=False：审计与业务变更同事务提交（同生共死）；
    越权拒绝等无业务事务的场景由调用方传 commit=True 立即落库。
    """
    log = RectificationAuditLog(
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
# 工具
# --------------------------------------------------------------------------- #

def _company_name(db: Session, company_id: int) -> str:
    company = db.get(Company, company_id)
    return company.name if company else str(company_id)


def _gen_order_no(db: Session, year: int) -> str:
    count = db.query(RectificationOrder).filter(RectificationOrder.year == year).count()
    return f"RC{year}{count + 1:06d}"


def _validate_due_date(due_date: str) -> str:
    due_date = (due_date or "").strip()
    if not due_date:
        return ""
    if not _DATE_RE.match(due_date):
        raise RectificationError("整改期限格式非法，应为 YYYY-MM-DD")
    try:
        datetime.strptime(due_date, "%Y-%m-%d")
    except ValueError:
        raise RectificationError("整改期限不是有效日期")
    return due_date


def get_order(db: Session, order_id: int) -> RectificationOrder:
    order = db.get(RectificationOrder, order_id)
    if order is None:
        raise RectificationError("整改工单不存在")
    return order


# --------------------------------------------------------------------------- #
# 开单
# --------------------------------------------------------------------------- #

def create_order(
    db: Session,
    *,
    company_id: int,
    year: int,
    title: str,
    description: str,
    operator: Operator,
    source_type: str = SOURCE_MANUAL,
    reconciliation_id: int | None = None,
    report_id: int | None = None,
    activity_id: int | None = None,
    discrepancy_codes: list[str] | None = None,
    due_date: str = "",
    idempotency_key: str | None = None,
) -> RectificationOrder:
    """监管/核查员开具整改工单，并校验来源单据真实且归属该企业年度。"""
    if source_type not in SOURCE_TYPES:
        raise RectificationError("问题来源类型非法")
    if not db.get(Company, company_id):
        raise RectificationError("企业不存在")
    due_date = _validate_due_date(due_date)

    recon = report = activity = None
    if source_type == SOURCE_RECONCILIATION:
        if not reconciliation_id:
            raise RectificationError("来源为对账差异时必须指定对账运行记录")
        recon = db.get(LedgerReconciliation, reconciliation_id)
        if recon is None:
            raise RectificationError("对账运行记录不存在")
        # 全量/年度运行可针对任意企业；企业范围运行必须与开单企业一致
        if recon.company_id is not None and recon.company_id != company_id:
            raise RectificationError("对账运行记录不属于该企业，不能据此开单")
        if recon.year is not None and recon.year != year:
            raise RectificationError("对账运行年度与工单年度不一致")
        # 圈定的差异 code 必须真实出现在该次运行的差异明细里
        valid_codes = {
            d.get("code") for d in json.loads(recon.discrepancies_json or "[]")
        }
        codes = discrepancy_codes or []
        unknown = [c for c in codes if c not in valid_codes]
        if unknown:
            raise RectificationError(f"对账差异代码在该次运行中不存在：{', '.join(unknown)}")
    elif source_type == SOURCE_REPORT:
        if not report_id:
            raise RectificationError("来源为报告问题时必须指定 MRV 报告")
        report = db.get(MrvReport, report_id)
        if report is None:
            raise RectificationError("MRV 报告不存在")
        if report.company_id != company_id or report.year != year:
            raise RectificationError("报告不属于该企业年度，不能据此开单")
    elif source_type == SOURCE_ACTIVITY:
        if not activity_id:
            raise RectificationError("来源为活动数据问题时必须指定活动数据")
        activity = db.get(ActivityData, activity_id)
        if activity is None:
            raise RectificationError("活动数据不存在")
        if activity.company_id != company_id or activity.year != year:
            raise RectificationError("活动数据不属于该企业年度，不能据此开单")

    order = RectificationOrder(
        order_no=_gen_order_no(db, year),
        company_id=company_id,
        year=year,
        status=OPEN,
        title=title.strip(),
        description=description.strip(),
        source_type=source_type,
        reconciliation_id=recon.id if recon else None,
        report_id=report.id if report else None,
        activity_id=activity.id if activity else None,
        discrepancy_codes=json.dumps(discrepancy_codes or [], ensure_ascii=False),
        due_date=due_date,
        issued_by=operator.id,
        idempotency_key=idempotency_key,
    )
    db.add(order)
    db.flush()
    write_audit(
        db, operator, "order.create",
        target_id=order.id, order_id=order.id,
        detail=f"开具整改工单 {order.order_no}（{_company_name(db, company_id)} {year}年度，"
               f"来源 {source_type}）：{title.strip()}",
    )
    db.commit()
    db.refresh(order)
    return order


# --------------------------------------------------------------------------- #
# 企业提交整改
# --------------------------------------------------------------------------- #

def submit_rectification(
    db: Session,
    order_id: int,
    *,
    company_id: int,
    operator: Operator,
    rectification_measure: str,
    evidences: list[dict],
    emission_adjustment: float | None = None,
) -> RectificationOrder:
    """企业提交整改措施与证据；仅 open/rejected 状态可提交。"""
    order = get_order(db, order_id)
    if order.company_id != company_id:
        raise RectificationError("无权整改其他企业的工单")
    if order.status in TERMINAL_STATUSES:
        raise RectificationError("工单已终结，不能再提交整改")
    if order.status == SUBMITTED:
        raise RectificationError("整改已提交，正在等待核查员审核")

    measure = (rectification_measure or "").strip()
    if len(measure) < 2:
        raise RectificationError("请填写整改措施说明（至少 2 个字符）")
    if not evidences:
        raise RectificationError("请至少上传或登记一条整改证据")

    next_round = int(order.submit_count or 0) + 1
    order.rectification_measure = measure
    order.emission_adjustment = (
        round(float(emission_adjustment), 4) if emission_adjustment is not None else None
    )
    order.status = SUBMITTED
    order.submitted_by = operator.id
    order.submitted_at = datetime.utcnow()
    order.submit_count = next_round

    for ev in evidences:
        db.add(RectificationEvidence(
            order_id=order.id,
            company_id=order.company_id,
            round=next_round,
            evidence_type=ev["evidence_type"],
            name=ev["name"].strip(),
            file_url=(ev.get("file_url") or "").strip(),
            remark=(ev.get("remark") or "").strip(),
            uploaded_by=operator.id,
        ))

    write_audit(
        db, operator, "order.submit",
        target_id=order.id, order_id=order.id,
        detail=f"企业提交整改工单 {order.order_no}（第 {next_round} 轮），"
               f"证据 {len(evidences)} 条"
               + (f"，申报排放调整 {float(emission_adjustment):.4f} tCO2e"
                  if emission_adjustment is not None else ""),
    )
    db.commit()
    db.refresh(order)
    return order


# --------------------------------------------------------------------------- #
# 核查员审核
# --------------------------------------------------------------------------- #

def _append_report_rectification_note(db: Session, order: RectificationOrder,
                                      operator: Operator, adjustment: float | None) -> None:
    """把整改结论 append 到关联 MRV 报告 report_json.rectifications（履约报告回写）。

    只追加整改认定记录，不改动报告排放快照与状态（快照纠正走冲正重批链路）。
    """
    report = None
    if order.report_id:
        report = db.get(MrvReport, order.report_id)
    if report is None:
        report = (
            db.query(MrvReport)
            .filter(MrvReport.company_id == order.company_id, MrvReport.year == order.year)
            .first()
        )
    if report is None:
        return

    try:
        detail = json.loads(report.report_json or "{}")
    except json.JSONDecodeError:
        detail = {}
    if not isinstance(detail, dict):
        detail = {}
    notes = detail.setdefault("rectifications", [])
    notes.append({
        "rectification_order_id": order.id,
        "order_no": order.order_no,
        "source_type": order.source_type,
        "reviewed_by": operator.username,
        "reviewed_at": datetime.utcnow().isoformat(),
        "measure": order.rectification_measure,
        "confirmed_emission_adjustment": adjustment,
        "comment": order.review_comment,
    })
    report.report_json = json.dumps(detail, ensure_ascii=False)


def review_order(
    db: Session,
    order_id: int,
    *,
    operator: Operator,
    approved: bool,
    comment: str,
    confirmed_emission_adjustment: float | None = None,
) -> RectificationOrder:
    """核查员审核：通过（approved=True）或驳回（False，工单回到 open）。

    通过时在同业务事务内回写履约报告整改结论，并在事务提交后自动运行
    企业+年度对账（对账只读、独立提交；失败只标记不影响审核结论）。
    """
    order = get_order(db, order_id)
    if order.status != SUBMITTED:
        raise RectificationError("仅已提交整改（待审核）的工单可审核")
    comment = (comment or "").strip()
    if len(comment) < 2:
        raise RectificationError("请填写审核意见（至少 2 个字符）")

    order.review_comment = comment
    order.reviewed_by = operator.id
    order.reviewed_at = datetime.utcnow()

    if not approved:
        order.status = REJECTED
        # 驳回即回到待整改：清空在审提交状态字段中的“当前在审”语义，
        # 但保留 measure/evidence 历史轨迹供企业对照修改
        write_audit(
            db, operator, "review.reject",
            target_id=order.id, order_id=order.id,
            detail=f"驳回整改工单 {order.order_no}（第 {order.submit_count} 轮）：{comment}",
        )
        db.commit()
        db.refresh(order)
        return order

    adjustment = (
        round(float(confirmed_emission_adjustment), 4)
        if confirmed_emission_adjustment is not None
        else (round(float(order.emission_adjustment), 4)
              if order.emission_adjustment is not None else None)
    )
    order.confirmed_emission_adjustment = adjustment
    order.status = APPROVED

    # 1) 履约报告回写（同事务）
    _append_report_rectification_note(db, order, operator, adjustment)
    write_audit(
        db, operator, "review.approve",
        target_id=order.id, order_id=order.id,
        detail=f"审核通过整改工单 {order.order_no}：{comment}"
               + (f"，认定排放调整 {adjustment:.4f} tCO2e" if adjustment is not None else ""),
    )
    db.commit()
    db.refresh(order)

    # 2) 对账差异回写：审核通过后自动复核该企业年度对账（只读、独立运行）。
    #    对账服务自行管理事务与幂等，异常不回滚审核结论，仅在工单上留痕待重跑。
    try:
        recon_no = f"RECT-{order.order_no}-{uuid.uuid4().hex[:8].upper()}"
        run = run_reconciliation(
            db,
            scope="company",
            company_id=order.company_id,
            year=order.year,
            idempotency_key=recon_no,
            triggered_by=operator.id,
        )
        order.writeback_reconciliation_id = run.id
        order.writeback_status = run.status
        order.writeback_discrepancy_count = int(run.discrepancy_count or 0)
        order.writeback_at = datetime.utcnow()
        db.commit()
        db.refresh(order)
        write_audit(
            db, operator, "review.writeback",
            target_id=order.id, order_id=order.id,
            detail=f"工单 {order.order_no} 通过后自动对账（{run.recon_no}）："
                   f"{run.status}，差异 {run.discrepancy_count} 项",
            commit=True,
        )
    except Exception as exc:  # noqa: BLE001 - 对账失败不应抹掉审核结论
        db.rollback()
        order = db.get(RectificationOrder, order_id)
        if order is not None:
            order.writeback_status = "failed"
            order.writeback_at = datetime.utcnow()
            db.commit()
            db.refresh(order)
            write_audit(
                db, operator, "review.writeback",
                target_id=order.id, order_id=order.id,
                detail=f"工单 {order.order_no} 通过后自动对账失败：{str(exc)[:300]}",
                result="denied",
                commit=True,
            )
    return order


def rerun_writeback(db: Session, order_id: int, *, operator: Operator) -> RectificationOrder:
    """对已通过但回写对账失败/监管要求复核的工单重跑企业年度对账。"""
    order = get_order(db, order_id)
    if order.status != APPROVED:
        raise RectificationError("仅审核通过的工单可重新回写对账")
    recon_no = f"RECT-{order.order_no}-R{uuid.uuid4().hex[:8].upper()}"
    run = run_reconciliation(
        db,
        scope="company",
        company_id=order.company_id,
        year=order.year,
        idempotency_key=recon_no,
        triggered_by=operator.id,
    )
    order.writeback_reconciliation_id = run.id
    order.writeback_status = run.status
    order.writeback_discrepancy_count = int(run.discrepancy_count or 0)
    order.writeback_at = datetime.utcnow()
    write_audit(
        db, operator, "review.writeback.rerun",
        target_id=order.id, order_id=order.id,
        detail=f"工单 {order.order_no} 重新对账（{run.recon_no}）：{run.status}，"
               f"差异 {run.discrepancy_count} 项",
    )
    db.commit()
    db.refresh(order)
    return order


# --------------------------------------------------------------------------- #
# 监管关闭
# --------------------------------------------------------------------------- #

def close_order(
    db: Session,
    order_id: int,
    *,
    operator: Operator,
    reason: str,
) -> RectificationOrder:
    """监管直接关闭工单（无需整改/重复开单等），须填原因；终态。"""
    order = get_order(db, order_id)
    if order.status in TERMINAL_STATUSES:
        raise RectificationError("工单已终结，不能重复关闭")
    reason = (reason or "").strip()
    if len(reason) < 2:
        raise RectificationError("请填写关闭原因（至少 2 个字符）")

    order.status = CLOSED
    order.close_reason = reason
    order.closed_by = operator.id
    order.closed_at = datetime.utcnow()
    write_audit(
        db, operator, "order.close",
        target_id=order.id, order_id=order.id,
        detail=f"关闭整改工单 {order.order_no}：{reason}",
    )
    db.commit()
    db.refresh(order)
    return order


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #

def list_orders(
    db: Session,
    *,
    company_id: int | None = None,
    year: int | None = None,
    status: str | None = None,
    source_type: str | None = None,
):
    q = db.query(RectificationOrder)
    if company_id is not None:
        q = q.filter(RectificationOrder.company_id == company_id)
    if year is not None:
        q = q.filter(RectificationOrder.year == year)
    if status:
        q = q.filter(RectificationOrder.status == status)
    if source_type:
        q = q.filter(RectificationOrder.source_type == source_type)
    return q.order_by(RectificationOrder.id.desc()).all()


def list_evidences(db: Session, order_id: int, *, round_no: int | None = None):
    q = db.query(RectificationEvidence).filter(RectificationEvidence.order_id == order_id)
    if round_no is not None:
        q = q.filter(RectificationEvidence.round == round_no)
    return q.order_by(RectificationEvidence.round.asc(), RectificationEvidence.id.asc()).all()


def list_audit_logs(db: Session, *, order_id: int | None = None, limit: int = 200):
    q = db.query(RectificationAuditLog)
    if order_id is not None:
        q = q.filter(RectificationAuditLog.order_id == order_id)
    return q.order_by(RectificationAuditLog.id.desc()).limit(limit).all()
