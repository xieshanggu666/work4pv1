"""碳排放整改工单模型：工单、证据材料、对账差异处置与监管审计。

业务链路：
监管（admin/verifier）或企业发起整改工单（可由统一对账差异一键转单）→
企业上传整改证据并提交整改说明 → 核查员审核（通过 / 驳回重改 / 关闭）→
审核通过的结果回写对账差异处置（resolved/waived）与履约（MRV）报告整改附注，
可选同事务重算排放量、刷新报告草稿并发起一次新对账验证差异是否消失；
全过程（含每一次越权拒绝）写监管审计日志。
"""

from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)

from app.core.database import Base


class CarbonRectificationOrder(Base):
    """碳排放整改工单：问题登记 → 企业整改举证 → 核查员审核/驳回/关闭。

    状态机：
    - open：已建单，待企业整改并提交（被驳回的工单也回到本状态）；
    - submitted：企业已提交整改说明与证据，待核查员审核；
    - approved：核查员审核通过（终态）。审核通过时按工单配置回写对账差异
      处置与履约报告整改附注，可选重算排放量并发起对账复验；
    - closed：核查员/监管关闭（终态，如问题不成立、免予整改），需关闭原因；
      若工单关联对账差异，关闭即把该差异处置标记为 waived。
    """

    __tablename__ = "carbon_rectification_orders"
    __table_args__ = (
        # 建单请求幂等：双击/超时重试只生成一张工单（NULL 不参与唯一约束）
        UniqueConstraint("idempotency_key", name="uq_rect_order_idem"),
        Index("ix_rect_order_company_status", "company_id", "status"),
    )

    id = Column(Integer, primary_key=True)
    order_no = Column(String(32), nullable=False, unique=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=True, index=True)
    # manual=手工登记；recon_discrepancy=由对账差异转单
    source = Column(String(24), nullable=False, default="manual", index=True)
    title = Column(String(200), nullable=False)
    # activity_data/emission_factor/calculation/report/reconciliation/quota/other
    issue_type = Column(String(24), nullable=False, default="other", index=True)
    description = Column(Text, nullable=False, default="")
    requirement = Column(Text, nullable=False, default="")          # 整改要求
    due_date = Column(String(10), nullable=False, default="")       # 整改期限 YYYY-MM-DD
    # open/submitted/approved/closed
    status = Column(String(16), nullable=False, default="open", index=True)

    created_by = Column(Integer, nullable=True)
    creator_role = Column(String(16), nullable=False, default="")

    # 企业整改提交内容（最新一次；驳回后重新提交覆盖），JSON：
    # {"summary": 整改情况说明, "measures": 整改措施, "impact": 排放/数据影响说明}
    submission_json = Column(Text, nullable=False, default="")
    submitted_by = Column(Integer, nullable=True)
    submitted_at = Column(DateTime, nullable=True)

    # 审核（通过/驳回）信息
    reviewed_by = Column(Integer, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    review_comment = Column(Text, nullable=False, default="")
    reject_reason = Column(Text, nullable=False, default="")
    rejected_at = Column(DateTime, nullable=True)
    rejection_count = Column(Integer, nullable=False, default=0)

    # 关闭信息（问题不成立/免予整改等）
    closed_by = Column(Integer, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    close_reason = Column(Text, nullable=False, default="")

    # 审核通过后的结构化回写结果，JSON：
    # {recalculated, emission_before, emission_after, report_annotated, report_id,
    #  resolution(fingerprint/status), discrepancy_still_present, followup_recon_run_id, ...}
    corrective_action_json = Column(Text, nullable=False, default="")

    # 对账差异转单的溯源信息（差异快照，对账运行本身不可变）
    recon_run_id = Column(Integer, ForeignKey("ledger_reconciliations.id"), nullable=True, index=True)
    recon_code = Column(String(48), nullable=False, default="")
    recon_refs_json = Column(Text, nullable=False, default="")
    recon_message = Column(Text, nullable=False, default="")
    # 关联的对账差异处置单；审核通过=resolved，关闭=waived
    resolution_id = Column(Integer, ForeignKey("recon_discrepancy_resolutions.id"), nullable=True)
    # 审核通过后发起的复验对账运行
    followup_recon_run_id = Column(Integer, ForeignKey("ledger_reconciliations.id"), nullable=True)

    idempotency_key = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class CarbonRectificationEvidence(Base):
    """整改证据材料：企业随整改提交的方案、报告与佐证文件登记。"""

    __tablename__ = "carbon_rectification_evidences"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("carbon_rectification_orders.id"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    # rectification_plan=整改方案；rectification_report=整改报告；
    # supporting_doc=佐证材料（台账/票据/照片等）；other=其他
    evidence_type = Column(String(24), nullable=False, default="supporting_doc")
    file_name = Column(String(256), nullable=False)
    file_url = Column(String(512), nullable=False, default="")
    file_hash = Column(String(128), nullable=False, default="")   # 内容哈希（SHA-256 等），防篡改
    file_size = Column(Integer, nullable=False, default=0)
    description = Column(Text, nullable=False, default="")
    uploaded_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class ReconDiscrepancyResolution(Base):
    """对账差异处置：整改工单审核结果向对账差异的回写凭据。

    ``fingerprint`` 是差异的稳定身份（差异代码 + 企业/年度范围 + 仅含身份的
    refs，剔除数值等易变内容），因此后续对账运行中再次出现同一条差异时可被
    识别为“已处置/已豁免但复发”，历史对账运行记录本身保持不可变。
    """

    __tablename__ = "recon_discrepancy_resolutions"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_recon_discrepancy_fp"),
        Index("ix_recon_resolution_company_year", "company_id", "year"),
    )

    id = Column(Integer, primary_key=True)
    # 差异首次发现并据此转单的对账运行
    recon_run_id = Column(Integer, ForeignKey("ledger_reconciliations.id"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)
    year = Column(Integer, nullable=True, index=True)
    discrepancy_code = Column(String(48), nullable=False)
    refs_json = Column(Text, nullable=False, default="{}")
    fingerprint = Column(String(88), nullable=False)
    # pending=工单处理中；resolved=审核通过并整改；waived=核查关闭免予整改/豁免
    status = Column(String(16), nullable=False, default="pending", index=True)
    order_id = Column(Integer, ForeignKey("carbon_rectification_orders.id"), nullable=True, index=True)
    comment = Column(Text, nullable=False, default="")
    resolved_by = Column(Integer, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class CarbonRectificationAuditLog(Base):
    """整改工单监管审计：建单/举证/提交/审核通过/驳回/关闭与越权拒绝均留痕。"""

    __tablename__ = "carbon_rectification_audit_logs"

    id = Column(Integer, primary_key=True)
    operator_id = Column(Integer, nullable=True, index=True)
    operator_name = Column(String(64), nullable=False, default="")
    operator_role = Column(String(16), nullable=False, default="")
    # order.create/evidence.upload/order.submit/order.approve/order.reject/
    # order.close/access.denied
    action = Column(String(64), nullable=False, index=True)
    target_type = Column(String(16), nullable=False, default="")
    target_id = Column(Integer, nullable=True)
    order_id = Column(Integer, nullable=True, index=True)
    detail = Column(String(500), nullable=False, default="")
    result = Column(String(16), nullable=False, default="success")  # success / denied
    ip = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
