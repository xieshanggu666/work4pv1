"""碳排放整改工单：监管/核查发现问题 → 企业整改举证 → 核查员审核 → 结果回写。

业务状态机
==========
整改工单（RectificationOrder）::

    open ──企业提交整改──▶ submitted ──审核通过──▶ approved（终态）
      ▲                      │
      └──── 驳回（重新整改）──┘
    open / submitted / rejected ──监管关闭──▶ closed（终态）

- open：监管或核查员开单，企业尚未提交整改方案与证据；
- submitted：企业已提交整改措施与证据材料，等待核查员审核；
- approved：核查员审核通过。同一事务内把整改结论（措施/排放调整量/审核意见）
  回写关联 MRV 报告与履约记录，并自动触发一次企业+年度范围对账，
  对账运行结论挂接工单（对账差异处置闭环）；
- rejected：核查员驳回并填写原因，工单回到 open，企业按意见重新整改提交，
  历次提交与审核轨迹全部保留；
- closed：监管在企业无需整改（如差异属系统误差、重复开单）等场景直接关闭，
  关闭必须填写原因，终态。

问题来源（source_type）
- reconciliation：统一对账发现差异（ledger_reconciliations.status=discrepancy）；
- report：MRV 报告核查问题（快照过期/排放数据存疑等）；
- activity：活动数据核验问题（错报、漏报、因子误用）；
- manual：监管/核查员巡检或其他途径手动开单。

审计：开单、提交、驳回、通过、关闭与每一次越权拒绝均写
rectification_audit_logs，形成完整监管审计轨迹。
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

# 工单状态
OPEN = "open"
SUBMITTED = "submitted"
APPROVED = "approved"
REJECTED = "rejected"
CLOSED = "closed"

# 终态：不可再流转
TERMINAL_STATUSES = (APPROVED, CLOSED)

# 问题来源
SOURCE_RECONCILIATION = "reconciliation"
SOURCE_REPORT = "report"
SOURCE_ACTIVITY = "activity"
SOURCE_MANUAL = "manual"
SOURCE_TYPES = (
    SOURCE_RECONCILIATION,
    SOURCE_REPORT,
    SOURCE_ACTIVITY,
    SOURCE_MANUAL,
)


class RectificationOrder(Base):
    """碳排放整改工单：企业排放数据/履约问题的整改任务单。"""

    __tablename__ = "rectification_orders"
    __table_args__ = (
        # 开单请求幂等：同一客户端重复提交（双击/超时重试）只生成一张工单
        UniqueConstraint("idempotency_key", name="uq_rectification_order_idem"),
        Index("ix_rectification_company_status", "company_id", "status"),
    )

    id = Column(Integer, primary_key=True)
    order_no = Column(String(32), nullable=False, unique=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    # open/submitted/approved/rejected/closed
    status = Column(String(16), nullable=False, default=OPEN, index=True)
    title = Column(String(200), nullable=False, default="")
    description = Column(Text, nullable=False, default="")
    # reconciliation/report/activity/manual
    source_type = Column(String(16), nullable=False, default=SOURCE_MANUAL, index=True)
    # 来源单据引用（均可空）：对账运行 / MRV 报告 / 活动数据
    reconciliation_id = Column(Integer, ForeignKey("ledger_reconciliations.id"), nullable=True, index=True)
    report_id = Column(Integer, ForeignKey("mrv_reports.id"), nullable=True, index=True)
    activity_id = Column(Integer, ForeignKey("activity_data.id"), nullable=True, index=True)
    # 开单时引用的差异明细（对账 discrepancies_json 中的 code 列表，JSON 存储）
    discrepancy_codes = Column(Text, nullable=False, default="[]")

    due_date = Column(String(10), nullable=False, default="")         # 整改期限 YYYY-MM-DD
    issued_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    issued_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # 企业最近一次整改提交
    rectification_measure = Column(Text, nullable=False, default="")  # 整改措施说明
    emission_adjustment = Column(Numeric(18, 4), nullable=True)       # 企业自查排放调整量（tCO2e，正=补报增排/负=核减）
    submitted_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    submit_count = Column(Integer, nullable=False, default=0)         # 累计提交次数

    # 核查员最近一次审核
    review_comment = Column(Text, nullable=False, default="")         # 审核意见（通过/驳回均填）
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    # 审核通过时认定的最终排放调整量（缺省取企业申报值；仅作整改结论登记，
    # 不直接改写已批准报告的履约排放快照——排放数据纠正仍走核验/重算/冲正重批链路）
    confirmed_emission_adjustment = Column(Numeric(18, 4), nullable=True)

    # 关闭（监管直接关闭，须填原因）
    close_reason = Column(Text, nullable=False, default="")
    closed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    closed_at = Column(DateTime, nullable=True)

    # 审核通过后回写：自动触发的企业+年度对账运行
    writeback_reconciliation_id = Column(Integer, ForeignKey("ledger_reconciliations.id"), nullable=True)
    writeback_status = Column(String(16), nullable=False, default="")  # balanced/discrepancy
    writeback_discrepancy_count = Column(Integer, nullable=True)
    writeback_at = Column(DateTime, nullable=True)

    idempotency_key = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class RectificationEvidence(Base):
    """整改证据材料：企业每次提交整改时上传/登记的佐证。

    驳回后重新提交会产生新一轮证据，历史证据不删除（按 round 区分），
    与工单状态轨迹共同构成完整举证链。
    """

    __tablename__ = "rectification_evidences"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("rectification_orders.id"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    # 提交轮次：首次提交为 1，每驳回重交一次 +1
    round = Column(Integer, nullable=False, default=1)
    # 证据类型：document（文件/台账）/ photo（影像）/ data（数据包）/ other
    evidence_type = Column(String(16), nullable=False, default="document")
    name = Column(String(200), nullable=False, default="")             # 材料名称
    # 文件 URL 或外部存储路径/台账编号；离线场景也可仅填引用说明
    file_url = Column(String(500), nullable=False, default="")
    remark = Column(String(500), nullable=False, default="")
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class RectificationAuditLog(Base):
    """整改监管审计：开单/提交/审核/驳回/关闭及越权拒绝全部留痕。"""

    __tablename__ = "rectification_audit_logs"

    id = Column(Integer, primary_key=True)
    operator_id = Column(Integer, nullable=True, index=True)
    operator_name = Column(String(64), nullable=False, default="")
    operator_role = Column(String(16), nullable=False, default="")
    # order.create/submit/review.approve/review.reject/close/access.denied
    action = Column(String(64), nullable=False, index=True)
    target_type = Column(String(16), nullable=False, default="order")
    target_id = Column(Integer, nullable=True)
    order_id = Column(Integer, nullable=True, index=True)
    detail = Column(String(500), nullable=False, default="")
    result = Column(String(16), nullable=False, default="success")  # success / denied
    ip = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
