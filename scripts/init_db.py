"""初始化数据库并写入演示数据。

用法：python scripts/init_db.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models import (  # noqa: E402
    ActivityData,
    AllowanceAccount,
    CalculationMethod,
    ComplianceRecord,
    Company,
    EmissionFactor,
    EmissionScope,
    Quota,
    User,
)
from app.services.auction_service import (  # noqa: E402
    Operator as AuctionOperator,
    create_session,
    open_session,
    place_bid,
)
from app.services.calculation_service import recalc_company_year  # noqa: E402
from app.services.loan_service import (  # noqa: E402
    Operator as LoanOperator,
    confirm_loan as confirm_quota_loan,
    create_loan,
    disburse_loan,
    repay_loan,
)
from app.services.mrv_service import (  # noqa: E402
    approve_report,
    generate_report,
    submit_report,
)
from app.services.quota_service import allocate_quota, clear_emission  # noqa: E402
from app.services.trading_service import transfer as transfer_allowance  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent


def main():
    if (BASE_DIR / "data" / "app.db").exists():
        (BASE_DIR / "data" / "app.db").unlink()
    Base.metadata.create_all(engine)
    db = SessionLocal()

    admin_hash, admin_salt = hash_password("123456")
    users = [
        User(username="admin", display_name="监管管理员", role="admin", password_hash=admin_hash, salt=admin_salt),
        User(username="verifier", display_name="第三方核查员", role="verifier", password_hash=admin_hash, salt=admin_salt),
    ]
    for u in users:
        db.add(u)
    db.flush()

    companies = [
        Company(code="ELEC-001", name="绿能电力集团", industry="电力", region="华东", boundary_desc="燃煤机组直接排放（范围一）与外购电力（范围二）"),
        Company(code="CEMT-001", name="恒固水泥股份", industry="水泥", region="华北", boundary_desc="窑炉工艺排放（范围一）与厂区用电（范围二）"),
    ]
    for c in companies:
        db.add(c)
    db.flush()

    q1_hash, q1_salt = hash_password("123456")
    db.add(User(username="elec", display_name="绿能电力", role="enterprise", company_id=companies[0].id, password_hash=q1_hash, salt=q1_salt))
    db.add(User(username="cement", display_name="恒固水泥", role="enterprise", company_id=companies[1].id, password_hash=q1_hash, salt=q1_salt))
    db.flush()

    scopes = [
        EmissionScope(company_id=companies[0].id, scope="1", category="燃料燃烧", name="燃煤机组直接排放", description="固定源燃料燃烧"),
        EmissionScope(company_id=companies[0].id, scope="2", category="外购电力", name="厂区外购电力", description="电网购入电力"),
        EmissionScope(company_id=companies[1].id, scope="1", category="工艺排放", name="窑炉工艺排放", description="水泥窑燃料与原料分解"),
        EmissionScope(company_id=companies[1].id, scope="2", category="外购电力", name="厂区外购电力", description="电网购入电力"),
    ]
    for s in scopes:
        db.add(s)
    db.flush()

    methods = [
        CalculationMethod(method_code="ELEC", name="外购电力排放因子法", scope="2", formula_type="activity_factor",
                          description="排放量 = 购电量 × 电网排放因子"),
        CalculationMethod(method_code="FUEL", name="燃料燃烧缺省值法", scope="1", formula_type="fuel_combustion",
                          params='{"carbon_oxidation": 0.98}',
                          description="排放量 = 燃料消耗量 × 综合排放系数 × 碳氧化率 × 44/12"),
        CalculationMethod(method_code="PROC", name="工艺过程排放法", scope="1", formula_type="activity_factor",
                          description="排放量 = 熟料产量 × 工艺排放因子"),
    ]
    db.add_all(methods)

    factors = [
        EmissionFactor(factor_code="ELEC-GRID", name="外购电力", scope="2", unit="tCO2/MWh", value=0.5703,
                       source="电网平均排放因子(2023)", valid_from="2024-01-01", valid_to="2025-12-31"),
        EmissionFactor(factor_code="COAL-PWR", name="燃煤消耗", scope="1", unit="tC/t", value=2.6,
                       source="燃料低位发热量与单位热值含碳量", valid_from="2024-01-01", valid_to=None),
        EmissionFactor(factor_code="CEMT-PROC", name="熟料产量", scope="1", unit="tCO2/t", value=0.82,
                       source="水泥行业工艺排放系数", valid_from="2024-01-01", valid_to=None),
    ]
    db.add_all(factors)

    year = 2025
    activities = [
        # 绿能电力 2025
        (companies[0].id, scopes[0].id, year, "quarterly", "燃煤消耗", "t", 120000, "燃料领用台账"),
        (companies[0].id, scopes[1].id, year, "monthly", "外购电力", "MWh", 88000, "电网结算单"),
        # 恒固水泥 2025
        (companies[1].id, scopes[2].id, year, "monthly", "熟料产量", "t", 650000, "生产统计月报"),
        (companies[1].id, scopes[3].id, year, "quarterly", "外购电力", "MWh", 46000, "电网结算单"),
    ]
    for cid, sid, y, period, atype, unit, qty, src in activities:
        db.add(ActivityData(company_id=cid, scope_id=sid, year=y, period=period,
                            activity_type=atype, unit=unit, quantity=qty, data_source=src, recorded_by=users[0].id, verified=1))
    db.commit()

    for c in companies:
        recalc_company_year(db, c.id, year)

    allocate_quota(db, companies[0].id, year, baseline=2200000, allocation_amount=1120000, adjustment=-20000)
    allocate_quota(db, companies[1].id, year, baseline=560000, allocation_amount=570000, adjustment=-10000)

    for c in companies:
        report = generate_report(db, c.id, year)
        submit_report(db, report)
        approve_report(db, report, verifier_id=users[1].id)

    # 绿能电力冻结后仍有缺口，买入配额后补缴；恒固水泥在批准冻结时即足额
    clear_emission(db, companies[0].id, year, f"{year}-12-31")
    elec_account = db.query(AllowanceAccount).filter_by(company_id=companies[0].id, year=year).one()
    elec_record = db.query(ComplianceRecord).filter_by(company_id=companies[0].id, year=year, is_active=1).one()
    transfer_allowance(
        db,
        elec_account,
        float(elec_record.deficit),
        "buy",
        counterparty="碳市场",
        tx_date=f"{year}-12-20",
    )
    clear_emission(db, companies[0].id, year, f"{year}-12-31")
    clear_emission(db, companies[1].id, year, f"{year}-12-31")

    # ---- 2026 年度：新一年度配额 + 碳配额集中竞价市场演示 ----
    next_year = year + 1
    allocate_quota(db, companies[0].id, next_year, baseline=1200000, allocation_amount=1200000)
    allocate_quota(db, companies[1].id, next_year, baseline=560000, allocation_amount=620000)

    auction_op = AuctionOperator(id=users[0].id, username="admin", role="admin")
    # 开放中的 2026 首场集中竞价（保留价 70 元/吨），供企业登录后直接报价
    open_auction = create_session(
        db,
        year=next_year,
        name=f"{next_year}年度首期碳配额集中竞价",
        reserve_price=70,
        estimated_volume=200000,
        operator=auction_op,
    )
    open_session(db, open_auction.id, auction_op)
    # 一张草稿场次，演示监管建场流程
    create_session(
        db,
        year=next_year,
        name=f"{next_year}年度第二期碳配额集中竞价（筹备中）",
        reserve_price=72,
        operator=auction_op,
    )

    # ---- 2026 年度配额借贷演示：恒固水泥向绿能电力借入 50,000 吨（已确认待放款）----
    demo_loan = create_loan(
        db,
        lender_id=companies[0].id,
        borrower_id=companies[1].id,
        year=next_year,
        amount=50000,
        price=5,
        initiator="borrower",
        due_date=f"{next_year}-11-30",
        remark="履约季临时周转借入",
    )
    confirm_quota_loan(db, demo_loan.id, companies[0].id)  # 出借方确认，冻结占用
    # 另一笔已放款并部分归还的借贷，展示在贷/清偿状态
    settled_loan = create_loan(
        db,
        lender_id=companies[0].id,
        borrower_id=companies[1].id,
        year=next_year,
        amount=20000,
        price=4,
        initiator="lender",
        due_date=f"{next_year}-10-31",
        remark="短期流动性支持",
    )
    confirm_quota_loan(db, settled_loan.id, companies[1].id)
    disburse_loan(db, settled_loan.id, companies[0].id)
    loan_op = LoanOperator(id=users[0].id, username="admin", role="admin")
    repay_loan(db, settled_loan.id, loan_op, amount=12000)

    # ---- 碳排放整改工单演示：一笔待整改、一笔已审核通过（已回写报告与对账）----
    from app.services.rectification_service import (
        Operator as RectificationOperator,
        create_order as create_rectification,
        review_order as review_rectification,
        submit_rectification as submit_rectification_demo,
    )

    rect_op = RectificationOperator(id=users[0].id, username="admin", role="admin")
    rect_verifier_op = RectificationOperator(id=users[1].id, username="verifier", role="verifier")
    rect_ent_op = RectificationOperator(id=None, username="elec", role="enterprise")

    # 恒固水泥 2025：待整改工单（企业登录后可见并提交证据）
    create_rectification(
        db,
        company_id=companies[1].id,
        year=year,
        title="熟料产量活动数据与生产月报口径不一致",
        description="监测计划边界与生产统计月报口径存在差异，请核实熟料产量活动量并提交佐证材料。",
        operator=rect_op,
        source_type="manual",
        due_date=f"{year}-11-30",
    )

    # 绿能电力 2025：完整流转（提交 → 通过 → 自动回写履约报告与企业年度对账）
    from app.models.report import MrvReport

    approved_report = (
        db.query(MrvReport)
        .filter_by(company_id=companies[0].id, year=year)
        .one()
    )
    approved_order = create_rectification(
        db,
        company_id=companies[0].id,
        year=year,
        title="外购电力排放因子适用年度存疑",
        description="部分外购电量疑似误用跨年度电网因子，请重新核对因子适用区间并整改。",
        operator=rect_verifier_op,
        source_type="report",
        report_id=approved_report.id,
        due_date=f"{year}-10-31",
    )
    submit_rectification_demo(
        db,
        approved_order.id,
        company_id=companies[0].id,
        operator=rect_ent_op,
        rectification_measure="已逐月核对电网结算单与因子适用区间，重算后确认适用因子无误，补充结算台账佐证。",
        emission_adjustment=0,
        evidences=[
            {"evidence_type": "document", "name": f"{year}年度电网结算汇总台账", "file_url": "", "remark": "逐月电量与因子"},
            {"evidence_type": "data", "name": "外购电重算工作底稿", "file_url": "", "remark": ""},
        ],
    )
    review_rectification(
        db,
        approved_order.id,
        operator=rect_verifier_op,
        approved=True,
        comment="佐证完整、重算口径正确，整改通过；结论已回写履约报告并完成企业年度对账复核。",
        confirmed_emission_adjustment=0,
    )

    db.commit()
    db.close()
    print(
        "初始化完成：2 家企业、4 个核算边界、3 个排放因子、4 条活动数据（2025）、2 份配额、"
        "2 份已批准 MRV 报告、2 条履约记录（含 1 次缺口补缴）、2026 年度配额、"
        "1 个开放竞价场次 + 1 个草稿场次、2 张配额借贷单（1 笔待放款、1 笔在贷部分归还）、"
        "2 张整改工单（1 笔待企业整改、1 笔审核通过并回写对账）"
    )
    print("账号：admin / verifier / elec / cement，密码均为 123456")


if __name__ == "__main__":
    main()
